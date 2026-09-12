import { ArrowRight, Container, FileCode2, GitBranch, LayoutTemplate } from "lucide-react";
import { useRouter } from "../router";
import type { ReactNode } from "react";
import { DeployWorkspace } from "../deploy/DeployWorkspace";
import { t } from "../i18n";
import type { DashboardPayload, Lang } from "../types";
import type { ComposeDeploymentDraft, DeployMode, ServiceManifestDraft } from "../deploy/types";
import type { DeploymentConfig } from "../deploymentConfigApi";
import type { DashboardViewModel } from "../dashboardViewModel";
import { PageHeader } from "./PageHeader";
import { Card, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";

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
  const { path, navigate } = useRouter();
  const source = path.split("/")[2] || "";
  if (!updating && !source) return <>
    <PageHeader meta={{ eyebrow: zh ? "应用 / 创建" : "Applications / Create", title: zh ? "创建应用" : "Create application", description: zh ? "选择配置来源，再校验和部署到集群。" : "Choose a configuration source, then validate and deploy.", metrics: [] }} />
    <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4" aria-label={zh ? "创建来源" : "Application source"}>
      {[
        { path: "/builds", icon: GitBranch, title: zh ? "Git 仓库" : "Git repository", description: zh ? "连接 GitHub、Gitea 或 Git URL，构建镜像并部署。" : "Connect GitHub, Gitea or a Git URL to build and deploy." },
        { path: "/create/image", icon: Container, title: zh ? "容器镜像" : "Container image", description: zh ? "配置现有镜像、网络、资源、环境变量和存储。" : "Configure an existing image, networking, resources and storage." },
        { path: "/create/yaml", icon: FileCode2, title: zh ? "YAML 文件" : "YAML documents", description: zh ? "编辑服务清单；Compose 应用可从模板入口开始。" : "Edit a service manifest. Start Compose applications from templates." },
        { path: "/create/templates", icon: LayoutTemplate, title: zh ? "应用模板" : "Application templates", description: zh ? "从单服务或 Compose 模板开始，保留所有高级配置。" : "Start from service or Compose templates with full configuration." },
      ].map((item) => {
        const Icon = item.icon;
        return (
          <Card key={item.path} className="h-full min-w-0">
            <CardHeader>
              <CardTitle>{item.title}</CardTitle>
              <CardDescription>{item.description}</CardDescription>
            </CardHeader>
            <CardFooter className="mt-auto">
              <Button variant="outline" onClick={() => navigate(item.path)} aria-label={`${zh ? "选择" : "Choose"} ${item.title}`}>
                <Icon data-icon="inline-start" />{zh ? "继续" : "Continue"}<ArrowRight data-icon="inline-end" />
              </Button>
            </CardFooter>
          </Card>
        );
      })}
    </section>
  </>;
  const title = updating && updateContext
    ? (zh ? `更新 ${updateContext.app.stack}` : `Update ${updateContext.app.stack}`)
    : (zh ? "创建应用" : "Create application");

  return (
    <>
      {!updating && source === "templates" && deployTemplateLanding ? (
        <PageHeader
          meta={{
            eyebrow: zh ? "部署工作台" : "Deploy workspace",
            title: zh ? "从模板创建" : "Create from a template",
            metrics: [],
            description: zh
              ? "选择应用模板，按需调整配置并部署到集群。"
              : "Choose an application template, customize it, and deploy to your cluster.",

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
        modalTitle={title}
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
