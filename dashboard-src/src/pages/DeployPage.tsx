import { useRouter } from "../router";
import type { ReactNode } from "react";
import { DeployWorkspace } from "../deploy/DeployWorkspace";
import { t } from "../i18n";
import type { DashboardPayload, Lang } from "../types";
import type { ComposeDeploymentDraft, DeployMode, ServiceManifestDraft } from "../deploy/types";
import type { DeploymentConfig } from "../deploymentConfigApi";
import type { DashboardViewModel } from "../dashboardViewModel";
import { PageHeader } from "./PageHeader";

export type DeployUpdateContext = {
  deployMode: DeployMode;
  app: {
    stack: string;
  };
  serviceDraft?: ServiceManifestDraft;
  composeDraft?: ComposeDeploymentDraft;
  deploymentConfig?: DeploymentConfig;
};

export function DeployPage({
  lang,
  token,
  payload,
  vm,
  updateContext,
  updateContextNode,
  onRefresh,
  onCloseUpdate,
  onTemplateLandingChange,
}: {
  lang: Lang;
  token: string;
  payload: DashboardPayload;
  vm: DashboardViewModel;
  updateContext: DeployUpdateContext | null;
  updateContextNode?: ReactNode;
  deployTemplateLanding: boolean;
  onRefresh: () => Promise<void> | void;
  onCloseUpdate?: () => void;
  onTemplateLandingChange: (isLanding: boolean) => void;
}) {
  const zh = lang === "zh";
  const updating = Boolean(updateContext);
  const { path, navigate } = useRouter();
  const source = path.split("/")[2] || "";
  if (!updating && !source) return <>
    <PageHeader meta={{ eyebrow: zh ? "应用 / 创建" : "Applications / Create", title: zh ? "创建应用" : "Create application", description: zh ? "选择配置来源，再校验和部署到集群。" : "Choose a configuration source, then validate and deploy.", metrics: [] }} />
    <section className="source-grid" aria-label={zh ? "创建来源" : "Application source"}>
      {[
        { path: "/builds", title: zh ? "Git 仓库" : "Git repository", description: zh ? "连接 GitHub、Gitea 或 Git URL，构建镜像并部署。" : "Connect GitHub, Gitea or a Git URL to build and deploy." },
        { path: "/create/image", title: zh ? "容器镜像" : "Container image", description: zh ? "配置现有镜像、网络、资源、环境变量和存储。" : "Configure an existing image, networking, resources and storage." },
        { path: "/create/yaml", title: zh ? "YAML 文件" : "YAML documents", description: zh ? "编辑服务清单；Compose 应用可从模板入口开始。" : "Edit a service manifest. Start Compose applications from templates." },
        { path: "/create/templates", title: zh ? "应用模板" : "Application templates", description: zh ? "从单服务或 Compose 模板开始，保留所有高级配置。" : "Start from service or Compose templates with full configuration." },
      ].map((item) => <button type="button" className="panel source-card" key={item.path} onClick={() => navigate(item.path)}><h2>{item.title}</h2><p>{item.description}</p><span>{zh ? "继续 →" : "Continue →"}</span></button>)}
    </section>
  </>;
  const title = updating && updateContext
    ? (zh ? `更新 ${updateContext.app.stack}` : `Update ${updateContext.app.stack}`)
    : (zh ? "创建应用" : "Create application");

  return (
    <>
      {!updating ? (
        <PageHeader
          meta={{
            eyebrow: zh ? "部署工作台" : "Deploy workspace",
            title: title,
            description: zh
              ? "模板、表单和 YAML 收敛在一个流程内，先校验再部署。"
              : "Templates, forms, and YAML stay in one flow with validation before deploy.",
            metrics: [
              { label: zh ? "单服务" : "Service", value: vm.deployServiceTemplates },
              { label: "Compose", value: vm.deployComposeTemplates },
              { label: "storageClass", value: vm.storageClasses.length },
            ],
          }}
        />
      ) : null}
      <DeployWorkspace
        key={updating ? updateContext?.app.stack : source}
        lang={lang}
        token={token}
        payload={payload}
        initialMode={updateContext?.deployMode}
        initialServiceDraft={updateContext?.serviceDraft}
        initialComposeDraft={updateContext?.composeDraft}
        initialServiceYaml={updateContext?.deployMode === "service" ? updateContext.deploymentConfig?.manifest : undefined}
        initialSidecarYaml={updateContext?.deployMode === "compose" ? updateContext.deploymentConfig?.manifest : undefined}
        initialComposeYaml={updateContext?.deployMode === "compose" ? updateContext.deploymentConfig?.composeContent : undefined}
        initialSourceName={updateContext?.deploymentConfig?.sourceName || undefined}
        initialEditorMode={updateContext?.deploymentConfig?.manifest || source === "yaml" ? "yaml" : "form"}
        initialYamlDirty={Boolean(updateContext?.deploymentConfig?.manifest)}
        contextLabel={updating && updateContext ? `${t(lang, "updateApp")} ${updateContext.app.stack}` : undefined}
        modalTitle={updating ? title : undefined}
        modalSubtitle={updating
          ? (zh ? "提交后按同名应用更新，部署前仍会先预览生成结果。" : "Deploying updates the same application. Preview is still available before submit.")
          : undefined}
        modalContext={updateContextNode}
        showTemplates={!updating && source === "templates"}
        onClose={updating ? onCloseUpdate : undefined}
        onRefresh={async () => {
          await onRefresh();
        }}
        onTemplateLandingChange={onTemplateLandingChange}
      />
    </>
  );
}
