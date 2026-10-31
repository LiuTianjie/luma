import { ArrowRight, Container, FileCode2, GitBranch, LayoutTemplate } from "@/components/icons";
import { spaLink, useRouter } from "../router";
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
  const { path, navigate } = useRouter();
  const source = path.split("/")[2] || "";
  if (!updating && !source) return <>
    <PageHeader meta={{ eyebrow: zh ? "应用 / 创建" : "Applications / Create", title: zh ? "创建应用" : "Create application", description: zh ? "选择配置来源，再校验和部署到集群。" : "Choose a configuration source, then validate and deploy.", metrics: [] }} />
    <section className="grid gap-4 sm:grid-cols-2" aria-label={zh ? "创建来源" : "Application source"}>
      {[
        { path: "/builds", icon: GitBranch, title: zh ? "从 Git 仓库构建" : "Build from a Git repository", description: zh ? "连接 GitHub、Gitea 或任意 Git URL，在构建节点生成镜像后部署。" : "Connect GitHub, Gitea or any Git URL. A build node produces the image, then it deploys.", hint: zh ? "推荐" : "Recommended" },
        { path: "/create/image", icon: Container, title: zh ? "部署容器镜像" : "Deploy a container image", description: zh ? "使用现有镜像，配置网络、资源、环境变量和存储。" : "Use an existing image and configure networking, resources, environment and storage." },
        { path: "/create/yaml", icon: FileCode2, title: zh ? "编写 YAML 清单" : "Write a YAML manifest", description: zh ? "直接编辑 Luma 服务清单；适合已有配置或需要完整控制。" : "Edit a Luma service manifest directly, for existing configs or full control." },
        { path: "/create/templates", icon: LayoutTemplate, title: zh ? "从模板开始" : "Start from a template", description: zh ? "单服务或 Compose 模板，保留所有高级配置项。" : "Service or Compose templates that keep every advanced option." },
      ].map((item) => {
        const Icon = item.icon;
        return (
          <a
            key={item.path}
            {...spaLink(item.path, navigate)}
            className="group relative flex min-w-0 items-start gap-4 rounded-lg bg-card p-5 shadow-xs ring-1 ring-border outline-none transition-[box-shadow,transform] hover:shadow-sm hover:ring-foreground/20 focus-visible:ring-2 focus-visible:ring-ring"
          >
            <span className="flex size-10 shrink-0 items-center justify-center rounded-lg bg-info-soft text-info-foreground ring-1 ring-info/15">
              <Icon className="size-5" aria-hidden="true" />
            </span>
            <span className="flex min-w-0 flex-1 flex-col gap-1">
              <span className="flex items-center gap-2">
                <span className="text-[0.9375rem] font-semibold">{item.title}</span>
                {item.hint ? <span className="rounded-full bg-secondary px-1.5 text-[0.6875rem] font-medium text-muted-foreground">{item.hint}</span> : null}
              </span>
              <span className="text-[0.8125rem]/5 text-muted-foreground">{item.description}</span>
            </span>
            <ArrowRight className="mt-0.5 size-4 shrink-0 text-subtle-foreground transition-transform group-hover:translate-x-0.5 group-hover:text-foreground" aria-hidden="true" />
          </a>
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
