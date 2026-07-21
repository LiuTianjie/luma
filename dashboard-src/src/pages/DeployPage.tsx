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
  deployTemplateLanding,
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
  const title = updating && updateContext
    ? (zh ? `更新 ${updateContext.app.stack}` : `Update ${updateContext.app.stack}`)
    : (zh ? "创建应用" : "Create application");

  return (
    <>
      <PageHeader
        meta={{
          eyebrow: updating ? (zh ? "应用更新" : "Application update") : (zh ? "部署工作台" : "Deploy workspace"),
          title: !deployTemplateLanding && !updating
            ? (zh ? "配置并部署" : "Configure and deploy")
            : title,
          description: updating
            ? (zh ? "沿用当前应用配置作为起点，提交时按同名应用更新。" : "Start from current configuration and update the same application.")
            : !deployTemplateLanding
              ? (zh ? "表单与 YAML 同步；校验通过后再部署。" : "Form and YAML stay in sync. Validate, then deploy.")
              : (zh ? "模板、表单和 YAML 收敛在一个流程内，先校验再部署。" : "Templates, forms, and YAML stay in one flow with validation before deploy."),
          metrics: [
            { label: zh ? "单服务" : "Service", value: vm.deployServiceTemplates },
            { label: "Compose", value: vm.deployComposeTemplates },
            { label: "storageClass", value: vm.storageClasses.length },
          ],
        }}
      />
      <DeployWorkspace
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
        initialEditorMode={updateContext?.deploymentConfig?.manifest ? "yaml" : "form"}
        initialYamlDirty={Boolean(updateContext?.deploymentConfig?.manifest)}
        contextLabel={updating && updateContext ? `${t(lang, "updateApp")} ${updateContext.app.stack}` : undefined}
        modalTitle={updating ? title : undefined}
        modalSubtitle={updating
          ? (zh ? "提交后按同名应用更新，部署前仍会先预览生成结果。" : "Deploying updates the same application. Preview is still available before submit.")
          : undefined}
        modalContext={updateContextNode}
        showTemplates={!updating}
        onClose={updating ? onCloseUpdate : undefined}
        onRefresh={async () => {
          await onRefresh();
          if (updating) onCloseUpdate?.();
        }}
        onTemplateLandingChange={onTemplateLandingChange}
      />
    </>
  );
}
