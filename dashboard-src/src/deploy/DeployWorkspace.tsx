import { ArrowLeft, FileCode2, ListChecks, Rocket } from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import type { DashboardPayload, Lang } from "../types";
import { ComposeDeployForm } from "./ComposeDeployForm";
import { deployStream, previewCompose, previewService } from "./deployApi";
import { DeploySummary } from "./DeploySummary";
import { DeployTemplates } from "./DeployTemplates";
import { GithubImportEntryCard, GithubImportPanel } from "./GithubImportPanel";
import { DEPLOY_TEMPLATES } from "./templates";
import type { ComposeDeploymentDraft, DeployMode, DeployPreviewResult, DeployStep, DeployTemplate, ServiceManifestDraft } from "./types";
import { findNode, hasReadyNodeInRegion, isReadyNode, nodesForRegion } from "./options";
import { composeDraftToSidecarYaml, serviceDraftToYaml, syncComposeYamlWithDraft } from "./yaml";
import { SingleServiceDeployForm } from "./SingleServiceDeployForm";
import { YamlPreviewEditor } from "./YamlPreviewEditor";

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function firstTemplate(mode: DeployMode) {
  return DEPLOY_TEMPLATES.find((template) => template.mode === mode) || DEPLOY_TEMPLATES[0];
}

function compact(values: Array<string | number | undefined | null | false>) {
  return values.filter((value) => value !== undefined && value !== null && value !== false && value !== "").join(" / ") || "-";
}

function currentConfigTitle(mode: DeployMode, serviceDraft: ServiceManifestDraft, composeDraft: ComposeDeploymentDraft) {
  return mode === "service" ? serviceDraft.name || "-" : composeDraft.name || "-";
}

function currentConfigFacts(mode: DeployMode, serviceDraft: ServiceManifestDraft, composeDraft: ComposeDeploymentDraft, lang: Lang) {
  if (mode === "service") {
    const publicTarget = serviceDraft.exposure === "none"
      ? (lang === "zh" ? "内部访问" : "internal only")
      : compact([serviceDraft.domain || "-", serviceDraft.port ? `:${serviceDraft.port}` : ""]);
    return [
      compact([serviceDraft.image]),
      compact([serviceDraft.region, serviceDraft.exposure]),
      publicTarget,
      `${serviceDraft.replicas} ${lang === "zh" ? "副本" : "replica"}`,
    ];
  }
  const exposed = composeDraft.services.filter((service) => service.exposure !== "none");
  return [
    `${composeDraft.services.length} ${lang === "zh" ? "服务" : "services"}`,
    compact([composeDraft.region, exposed.length ? exposed.map((service) => service.exposure).join(", ") : "none"]),
    exposed.length ? exposed.map((service) => `${service.name} -> ${service.domain || "-"}${service.port ? `:${service.port}` : ""}`).join(", ") : (lang === "zh" ? "内部访问" : "internal only"),
    `${composeDraft.volumes.length} ${lang === "zh" ? "卷" : "volumes"}`,
  ];
}

function deployFlowSteps(mode: DeployMode, lang: Lang) {
  const zh = lang === "zh";
  return mode === "service"
    ? [
      { id: "deploy-basic", label: zh ? "身份" : "Identity", value: "name / image" },
      { id: "deploy-network", label: zh ? "入口" : "Ingress", value: "domain / port" },
      { id: "deploy-runtime", label: zh ? "运行" : "Runtime", value: "cpu / env / volume" },
      { id: "deploy-advanced", label: zh ? "开关" : "Guardrails", value: "dns / nomad" },
    ]
    : [
      { id: "compose-basic", label: zh ? "应用" : "App", value: "compose" },
      { id: "compose-services", label: zh ? "服务" : "Services", value: "ingress / node" },
      { id: "compose-env", label: zh ? "密钥" : "Secrets", value: "env / secret" },
      { id: "compose-storage", label: zh ? "存储" : "Storage", value: "volume" },
      { id: "compose-advanced", label: zh ? "开关" : "Guardrails", value: "dns / nomad" },
    ];
}

type DashboardStorageClasses = NonNullable<NonNullable<DashboardPayload["storage"]>["storageClasses"]>;

function storageClassAllowsRegion(
  storageClass: DashboardStorageClasses[number] | undefined,
  region: ServiceManifestDraft["region"] | "",
) {
  const regions = storageClass?.regions || [];
  return !regions.length || !region || regions.includes(region);
}

function defaultStorageClassName(
  storageClasses: DashboardStorageClasses = [],
  region: ServiceManifestDraft["region"] | "" = "",
) {
  return storageClasses.find((storageClass) => storageClass.name && storageClassAllowsRegion(storageClass, region))?.name || "";
}

function hydrateStorageVolume<T extends { storageMode: string; storageClass: string }>(
  volume: T,
  region: ServiceManifestDraft["region"] | "",
  storageClasses: DashboardStorageClasses,
) {
  if (volume.storageMode !== "storageClass") return volume;
  const selected = storageClasses.find((storageClass) => storageClass.name === volume.storageClass);
  if (volume.storageClass && storageClassAllowsRegion(selected, region)) return volume;
  const fallback = defaultStorageClassName(storageClasses, region);
  if (fallback) return { ...volume, storageClass: fallback };
  return { ...volume, storageMode: "unmanaged", storageClass: "" };
}

function defaultNodeName(nodes: NonNullable<DashboardPayload["nodes"]>, region: ServiceManifestDraft["region"] | "") {
  return nodesForRegion(nodes, region)[0]?.name || "";
}

function hydrateServiceDraftDefaults(
  draft: ServiceManifestDraft,
  nodes: NonNullable<DashboardPayload["nodes"]>,
  storageClasses: DashboardStorageClasses,
) {
  const next = clone(draft);
  if ((next.exposure === "tailscale-relay" || next.exposure === "tcp-relay") && !next.node) {
    next.node = defaultNodeName(nodes, next.region);
  }
  next.volumeMounts = (next.volumeMounts || []).map((volume) => (
    hydrateStorageVolume(volume, next.region, storageClasses)
  ));
  return next;
}

function hydrateComposeDraftDefaults(
  draft: ComposeDeploymentDraft,
  nodes: NonNullable<DashboardPayload["nodes"]>,
  storageClasses: DashboardStorageClasses,
) {
  const next = clone(draft);
  next.services = (next.services || []).map((service) => (
    (service.exposure === "tailscale-relay" || service.exposure === "tcp-relay") && !service.node
      ? { ...service, node: defaultNodeName(nodes, service.region || next.region) }
      : service
  ));
  next.volumes = (next.volumes || []).map((volume) => (
    hydrateStorageVolume(volume, next.region, storageClasses)
  ));
  return next;
}

const secretRefPattern = /^\$\{[A-Za-z_][A-Za-z0-9_]*\}$/;
function isPositiveInteger(value: string | number) {
  const numeric = Number(value);
  return Number.isInteger(numeric) && numeric > 0;
}

function exposureRegionError(exposure: ServiceManifestDraft["exposure"], region: ServiceManifestDraft["region"] | "", label: string, lang: Lang) {
  if (exposure === "cn-edge" && region !== "cn") return lang === "zh" ? `${label} exposure=cn-edge 必须使用 region=cn` : `${label} exposure=cn-edge requires region=cn`;
  if (exposure === "external-edge" && region !== "global") return lang === "zh" ? `${label} exposure=external-edge 必须使用 region=global` : `${label} exposure=external-edge requires region=global`;
  if (exposure === "tailscale-relay" && region !== "home") return lang === "zh" ? `${label} exposure=tailscale-relay 必须使用 region=home` : `${label} exposure=tailscale-relay requires region=home`;
  return "";
}

function regionNodeErrors(nodes: NonNullable<DashboardPayload["nodes"]>, region: ServiceManifestDraft["region"] | "", nodeName: string, label: string, lang: Lang) {
  if (!nodes.length || !region) return [];
  const errors = [];
  if (!hasReadyNodeInRegion(nodes, region)) errors.push(lang === "zh" ? `${label} region=${region} 当前没有 ready/active 节点` : `${label} region=${region} has no ready/active nodes`);
  if (nodeName) {
    const node = findNode(nodes, nodeName);
    if (!node) errors.push(lang === "zh" ? `${label} 节点 ${nodeName} 不在当前节点清单中` : `${label} node ${nodeName} is not in the current node list`);
    else {
      if (!isReadyNode(node)) errors.push(lang === "zh" ? `${label} 节点 ${nodeName} 当前不是 ready/active` : `${label} node ${nodeName} is not ready/active`);
      if (node.region !== region) errors.push(lang === "zh" ? `${label} 节点 ${nodeName} 属于 region=${node.region || "-"}，不能用于 region=${region}` : `${label} node ${nodeName} belongs to region=${node.region || "-"} and cannot be used for region=${region}`);
    }
  }
  return errors;
}

function serviceErrors(draft: ServiceManifestDraft, yamlDirty: boolean, serviceYaml: string, nodes: NonNullable<DashboardPayload["nodes"]>, lang: Lang) {
  const serviceLabel = lang === "zh" ? "服务" : "service";
  if (yamlDirty) return serviceYaml.trim() ? [] : [lang === "zh" ? "service.yaml 不能为空" : "service.yaml cannot be empty"];
  const errors = [];
  if (!draft.name.trim()) errors.push(lang === "zh" ? "服务名不能为空" : "Service name cannot be empty");
  if (!draft.image.trim()) errors.push(lang === "zh" ? "镜像不能为空" : "Image cannot be empty");
  const regionError = exposureRegionError(draft.exposure, draft.region, draft.name || serviceLabel, lang);
  if (regionError) errors.push(regionError);
  errors.push(...regionNodeErrors(nodes, draft.region, draft.node, draft.name || serviceLabel, lang));
  if (draft.exposure !== "none" && !draft.domain.trim()) errors.push(lang === "zh" ? "公开入口必须填写域名" : "Public exposure requires a domain");
  if (draft.exposure !== "none" && !draft.port.trim()) errors.push(lang === "zh" ? "公开入口必须填写容器端口" : "Public exposure requires a container port");
  if (draft.exposure !== "none" && draft.port.trim() && !isPositiveInteger(draft.port)) errors.push(lang === "zh" ? "容器端口必须是正整数" : "Container port must be a positive integer");
  if (draft.publishPort.trim() && !isPositiveInteger(draft.publishPort)) errors.push(lang === "zh" ? "发布端口必须是正整数" : "Published port must be a positive integer");
  if (!isPositiveInteger(draft.replicas)) errors.push(lang === "zh" ? "副本数必须是大于 0 的整数" : "Replicas must be an integer greater than 0");
  for (const volume of draft.volumeMounts || []) {
    if (!volume.name.trim() && volume.target.trim()) errors.push(lang === "zh" ? "卷挂载缺少 volume 名称" : "Volume mount is missing a volume name");
    if (volume.name.trim() && !volume.target.trim()) errors.push(lang === "zh" ? `${volume.name.trim()} 缺少挂载目标` : `${volume.name.trim()} is missing a mount target`);
    if (volume.storageMode === "storageClass" && volume.name.trim() && !volume.storageClass.trim()) {
      errors.push(lang === "zh" ? `${volume.name.trim()} 必须选择 storageClass` : `${volume.name.trim()} must select a storageClass`);
    }
  }
  for (const row of draft.env || []) {
    if (!row.key.trim() && row.value.trim()) errors.push(lang === "zh" ? "环境变量缺少名称" : "Environment variable is missing a name");
    if (row.kind === "secret" && row.key.trim() && !secretRefPattern.test(row.value.trim())) {
      errors.push(lang === "zh" ? `${row.key.trim()} 密钥值必须使用 \${NAME} 引用` : `${row.key.trim()} secret value must use a \${NAME} reference`);
    }
  }
  return errors;
}

function composeErrors(draft: ComposeDeploymentDraft, yamlDirty: boolean, composeYaml: string, sidecarYaml: string, nodes: NonNullable<DashboardPayload["nodes"]>, lang: Lang) {
  if (yamlDirty) {
    const errors = [];
    if (!composeYaml.trim()) errors.push(lang === "zh" ? "docker-compose.yml 不能为空" : "docker-compose.yml cannot be empty");
    if (!sidecarYaml.trim()) errors.push(lang === "zh" ? "luma.compose.yml 不能为空" : "luma.compose.yml cannot be empty");
    return errors;
  }
  const errors = [];
  if (!draft.name.trim()) errors.push(lang === "zh" ? "应用名不能为空" : "Application name cannot be empty");
  errors.push(...regionNodeErrors(nodes, draft.region, "", draft.name || (lang === "zh" ? "应用" : "application"), lang));
  for (const service of draft.services) {
    const region = service.region || draft.region;
    const regionError = exposureRegionError(service.exposure, region, service.name, lang);
    if (regionError) errors.push(regionError);
    errors.push(...regionNodeErrors(nodes, region, service.node, service.name, lang));
    if (service.exposure !== "none" && !service.domain.trim()) errors.push(lang === "zh" ? `${service.name} 必须填写域名` : `${service.name} requires a domain`);
    if (service.exposure !== "none" && !service.port.trim()) errors.push(lang === "zh" ? `${service.name} 必须填写容器端口` : `${service.name} requires a container port`);
    if (service.exposure !== "none" && service.port.trim() && !isPositiveInteger(service.port)) errors.push(lang === "zh" ? `${service.name} 容器端口必须是正整数` : `${service.name} container port must be a positive integer`);
    if (service.publishPort.trim() && !isPositiveInteger(service.publishPort)) errors.push(lang === "zh" ? `${service.name} 发布端口必须是正整数` : `${service.name} published port must be a positive integer`);
    if (!isPositiveInteger(service.replicas)) errors.push(lang === "zh" ? `${service.name} 副本数必须是大于 0 的整数` : `${service.name} replicas must be an integer greater than 0`);
    for (const row of service.env || []) {
      if (!row.key.trim() && row.value.trim()) errors.push(lang === "zh" ? `${service.name} 环境变量缺少名称` : `${service.name} environment variable is missing a name`);
      if (row.kind === "secret" && row.key.trim() && !secretRefPattern.test(row.value.trim())) {
        errors.push(lang === "zh" ? `${service.name}.${row.key.trim()} 密钥值必须使用 \${NAME} 引用` : `${service.name}.${row.key.trim()} secret value must use a \${NAME} reference`);
      }
    }
  }
  for (const volume of draft.volumes) {
    if (volume.storageMode === "storageClass" && !volume.storageClass) errors.push(lang === "zh" ? `${volume.name} 必须选择 storageClass` : `${volume.name} must select a storageClass`);
    if (volume.storageMode === "local" && (!volume.localNode || !volume.localPath)) errors.push(lang === "zh" ? `${volume.name} 必须填写本地节点和路径` : `${volume.name} must specify a local node and path`);
    if (volume.storageMode === "local" && volume.localNode) {
      const node = findNode(nodes, volume.localNode);
      if (!node) errors.push(lang === "zh" ? `${volume.name} 本地存储节点 ${volume.localNode} 不在当前节点清单中` : `${volume.name} local storage node ${volume.localNode} is not in the current node list`);
      else if (!isReadyNode(node) || node.agentStatus !== "ready") {
        errors.push(lang === "zh" ? `${volume.name} 本地存储节点 ${volume.localNode} 不是 ready agent 节点` : `${volume.name} local storage node ${volume.localNode} is not a ready agent node`);
      }
    }
  }
  return errors;
}

export function DeployWorkspace({
  lang,
  token,
  payload,
  onRefresh,
  initialMode,
  initialServiceDraft,
  initialComposeDraft,
  initialServiceYaml,
  initialSidecarYaml,
  initialComposeYaml,
  initialSourceName,
  initialEditorMode,
  initialYamlDirty,
  contextLabel,
  modalTitle,
  modalSubtitle,
  modalContext,
  showTemplates = true,
  onClose,
  onTemplateLandingChange,
}: {
  lang: Lang;
  token: string;
  payload: DashboardPayload | null;
  onRefresh: () => Promise<void> | void;
  initialMode?: DeployMode;
  initialServiceDraft?: ServiceManifestDraft;
  initialComposeDraft?: ComposeDeploymentDraft;
  initialServiceYaml?: string;
  initialSidecarYaml?: string;
  initialComposeYaml?: string;
  initialSourceName?: string;
  initialEditorMode?: "form" | "yaml";
  initialYamlDirty?: boolean;
  contextLabel?: string;
  modalTitle?: string;
  modalSubtitle?: string;
  modalContext?: ReactNode;
  showTemplates?: boolean;
  onClose?: () => void;
  onTemplateLandingChange?: (isLanding: boolean) => void;
}) {
  const initial = initialMode === "compose" ? firstTemplate("compose") : firstTemplate("service");
  const [mode, setMode] = useState<DeployMode>(initialMode || "service");
  const [activeTemplateId, setActiveTemplateId] = useState(initial.id);
  const [serviceDraft, setServiceDraft] = useState<ServiceManifestDraft>(() => clone(initialServiceDraft || firstTemplate("service").service!));
  const [composeDraft, setComposeDraft] = useState<ComposeDeploymentDraft>(() => clone(initialComposeDraft || firstTemplate("compose").compose!));
  const [serviceYaml, setServiceYaml] = useState(() => initialServiceYaml || serviceDraftToYaml(clone(initialServiceDraft || firstTemplate("service").service!)));
  const [sidecarYaml, setSidecarYaml] = useState(() => initialSidecarYaml || composeDraftToSidecarYaml(clone(initialComposeDraft || firstTemplate("compose").compose!)));
  const [composeYaml, setComposeYaml] = useState(() => initialComposeYaml || clone((initialComposeDraft || firstTemplate("compose").compose!).dockerComposeYaml));
  const [sourceName, setSourceName] = useState(initialSourceName || (initialMode === "compose" ? "luma.compose.yml" : "service.yaml"));
  const [editorMode, setEditorMode] = useState<"form" | "yaml">(initialEditorMode || "form");
  const [templateLanding, setTemplateLanding] = useState(showTemplates && !initialMode && !initialServiceDraft && !initialComposeDraft && !initialServiceYaml && !initialSidecarYaml && !initialComposeYaml);
  const [yamlDirty, setYamlDirty] = useState(Boolean(initialYamlDirty));
  const [preview, setPreview] = useState<DeployPreviewResult | null>(null);
  const [steps, setSteps] = useState<DeployStep[]>([]);
  const [status, setStatus] = useState<"idle" | "previewing" | "deploying">("idle");
  const [runtimeErrors, setRuntimeErrors] = useState<string[]>([]);
  const [importView, setImportView] = useState(false);
  const nodes = payload?.nodes || [];
  const storageClasses = payload?.storage?.storageClasses || [];

  useEffect(() => {
    onTemplateLandingChange?.(templateLanding);
  }, [onTemplateLandingChange, templateLanding]);

  useEffect(() => {
    if (!initialMode && !initialServiceDraft && !initialComposeDraft && !initialServiceYaml && !initialSidecarYaml && !initialComposeYaml) return;
    setMode(initialMode || "service");
    setActiveTemplateId("current-application");
    setPreview(null);
    setSteps([]);
    setRuntimeErrors([]);
    setYamlDirty(Boolean(initialYamlDirty));
    setTemplateLanding(false);
    setEditorMode(initialEditorMode || "form");
    setSourceName(initialSourceName || (initialMode === "compose" ? "luma.compose.yml" : "service.yaml"));
    if (initialServiceDraft) {
      const next = clone(initialServiceDraft);
      setServiceDraft(next);
      setServiceYaml(initialServiceYaml || serviceDraftToYaml(next));
    } else if (initialServiceYaml) {
      setServiceYaml(initialServiceYaml);
    }
    if (initialComposeDraft) {
      const next = clone(initialComposeDraft);
      setComposeDraft(next);
      setComposeYaml(initialComposeYaml || next.dockerComposeYaml);
      setSidecarYaml(initialSidecarYaml || composeDraftToSidecarYaml(next));
    } else {
      if (initialComposeYaml) setComposeYaml(initialComposeYaml);
      if (initialSidecarYaml) setSidecarYaml(initialSidecarYaml);
    }
  }, [initialMode, initialServiceDraft, initialComposeDraft, initialServiceYaml, initialSidecarYaml, initialComposeYaml, initialSourceName, initialEditorMode, initialYamlDirty]);

  const validationErrors = useMemo(
    () => mode === "service"
      ? serviceErrors(serviceDraft, yamlDirty, serviceYaml, nodes, lang)
      : composeErrors(composeDraft, yamlDirty, composeYaml, sidecarYaml, nodes, lang),
    [composeDraft, composeYaml, lang, mode, nodes, serviceDraft, serviceYaml, sidecarYaml, yamlDirty],
  );
  const allErrors = [...validationErrors, ...runtimeErrors];
  const configTitle = currentConfigTitle(mode, serviceDraft, composeDraft);
  const configFacts = currentConfigFacts(mode, serviceDraft, composeDraft, lang);
  const flowSteps = deployFlowSteps(mode, lang);

  const selectTemplate = (template: DeployTemplate) => {
    setActiveTemplateId(template.id);
    setMode(template.mode);
    setTemplateLanding(false);
    setEditorMode("form");
    setPreview(null);
    setSteps([]);
    setRuntimeErrors([]);
    setYamlDirty(false);
    setSourceName(template.mode === "compose" ? "luma.compose.yml" : "service.yaml");
    if (template.mode === "service" && template.service) {
      const next = hydrateServiceDraftDefaults(template.service, nodes, storageClasses);
      setServiceDraft(next);
      setServiceYaml(serviceDraftToYaml(next));
    }
    if (template.mode === "compose" && template.compose) {
      const next = hydrateComposeDraftDefaults(template.compose, nodes, storageClasses);
      setComposeDraft(next);
      setComposeYaml(next.dockerComposeYaml);
      setSidecarYaml(composeDraftToSidecarYaml(next));
    }
  };

  const backToTemplates = () => {
    setTemplateLanding(true);
    setPreview(null);
    setSteps([]);
    setRuntimeErrors([]);
  };

  const changeMode = (nextMode: DeployMode) => {
    const template = firstTemplate(nextMode);
    setMode(nextMode);
    setActiveTemplateId(template.id);
    setPreview(null);
    setSteps([]);
    setRuntimeErrors([]);
  };

  const updateServiceDraft = (next: ServiceManifestDraft) => {
    setServiceDraft(next);
    if (!yamlDirty) setServiceYaml(serviceDraftToYaml(next));
  };

  const updateComposeDraft = (next: ComposeDeploymentDraft) => {
    const normalized = syncComposeYamlWithDraft(next);
    setComposeDraft(normalized);
    if (!yamlDirty) {
      setComposeYaml(normalized.dockerComposeYaml);
      setSidecarYaml(composeDraftToSidecarYaml(normalized));
    }
  };

  const runPreview = async () => {
    setRuntimeErrors([]);
    setPreview(null);
    if (validationErrors.length) return;
    setStatus("previewing");
    try {
      const result = mode === "service"
        ? await previewService({ token, manifest: serviceYaml, sourceName, skipDns: serviceDraft.skipDns, skipOrchestrator: serviceDraft.skipOrchestrator })
        : await previewCompose({ token, manifest: sidecarYaml, composeContent: composeYaml, sourceName, skipDns: composeDraft.skipDns, skipOrchestrator: composeDraft.skipOrchestrator });
      setPreview(result);
    } catch (error) {
      setRuntimeErrors([String(error instanceof Error ? error.message : error)]);
    } finally {
      setStatus("idle");
    }
  };

  const runDeploy = async () => {
    setRuntimeErrors([]);
    setSteps([]);
    if (validationErrors.length) return;
    const target = mode === "service" ? serviceDraft.name : composeDraft.name;
    const confirmMessage = lang === "zh"
      ? `确认部署 ${target} 到当前 Luma Control 集群？`
      : `Deploy ${target} to the current Luma Control cluster?`;
    if (!window.confirm(confirmMessage)) return;
    setStatus("deploying");
    try {
      await deployStream(
        mode === "service"
          ? { token, manifest: serviceYaml, sourceName, skipDns: serviceDraft.skipDns, skipOrchestrator: serviceDraft.skipOrchestrator }
          : { token, manifest: sidecarYaml, composeContent: composeYaml, sourceName, skipDns: composeDraft.skipDns, skipOrchestrator: composeDraft.skipOrchestrator },
        mode,
        (step) => setSteps((current) => [...current, step]),
      );
      await onRefresh();
    } catch (error) {
      setRuntimeErrors([String(error instanceof Error ? error.message : error)]);
    } finally {
      setStatus("idle");
    }
  };

  return (
    <section className={`deploy-workspace-panel ${modalTitle ? "modal-deploy-workspace" : ""}`}>
      {importView ? (
        <GithubImportPanel
          lang={lang}
          token={token}
          nodes={nodes}
          build={payload?.build}
          onBack={showTemplates ? () => setImportView(false) : undefined}
          onRefresh={onRefresh}
        />
      ) : (
      <>
      {!templateLanding ? <div className="panel-heading deploy-heading">
        <div>
          <p className="eyebrow">{templateLanding ? (lang === "zh" ? "模板库" : "Template gallery") : (lang === "zh" ? "配置应用" : "Configure application")}</p>
          <h2>{modalTitle || (templateLanding ? (lang === "zh" ? "选择一个模板开始" : "Choose a template to start") : (lang === "zh" ? "配置并部署" : "Configure and deploy"))}</h2>
          {modalSubtitle || contextLabel ? <small className="deploy-context-label">{modalSubtitle || contextLabel}</small> : null}
        </div>
        <div className="deploy-heading-actions">
          {!templateLanding && showTemplates ? (
            <button type="button" className="ghost" onClick={backToTemplates}>
              <ArrowLeft size={16} aria-hidden="true" />
              {lang === "zh" ? "返回模板" : "Back to templates"}
            </button>
          ) : null}
          {!templateLanding ? (
            <div className="deploy-editor-tabs">
              <button type="button" className={editorMode === "form" ? "active" : ""} onClick={() => setEditorMode("form")}>
                <ListChecks size={15} aria-hidden="true" />
                {lang === "zh" ? "配置表单" : "Form"}
              </button>
              <button type="button" className={editorMode === "yaml" ? "active" : ""} onClick={() => setEditorMode("yaml")}>
                <FileCode2 size={15} aria-hidden="true" />
                {lang === "zh" ? "YAML 文件" : "YAML files"}
              </button>
            </div>
          ) : null}
          {onClose ? <button type="button" className="icon-button" onClick={onClose}>{lang === "zh" ? "关闭" : "Close"}</button> : null}
        </div>
      </div> : null}
      {modalContext}
      {showTemplates && templateLanding ? (
        <>
          <GithubImportEntryCard lang={lang} onOpen={() => setImportView(true)} />
          <DeployTemplates lang={lang} mode={mode} templates={DEPLOY_TEMPLATES} activeId={activeTemplateId} onModeChange={changeMode} onSelect={selectTemplate} />
        </>
      ) : null}
      {templateLanding ? (
        <div className="template-gallery-footer">
          <div>
            <strong>{lang === "zh" ? "模板只会填充配置，不会自动部署。" : "Templates only prefill configuration. Nothing deploys automatically."}</strong>
            <span>{lang === "zh" ? "点击模板卡片后进入表单页面，可随时切换 YAML 视图。" : "Click a template to continue to the form page, where YAML view remains available."}</span>
          </div>
          <button type="button" className="ghost" onClick={() => selectTemplate(firstTemplate(mode))}>{lang === "zh" ? "使用当前推荐" : "Use recommended"}</button>
        </div>
      ) : (
        <>
          {showTemplates ? (
            <div className="selected-template-strip">
              <div>
                <span>{lang === "zh" ? "当前配置" : "Current config"}</span>
                <strong>{configTitle}</strong>
                <small>{mode === "service" ? (lang === "zh" ? "单服务" : "Single service") : "Compose"}</small>
              </div>
              <div className="selected-template-facts">
                {configFacts.map((fact) => <small key={fact}>{fact}</small>)}
              </div>
            </div>
          ) : null}
          {editorMode === "form" ? (
            <nav className="deploy-step-rail" aria-label={lang === "zh" ? "配置分段" : "Configuration sections"}>
              {flowSteps.map((step, index) => (
                <a href={`#${step.id}`} key={step.id}>
                  <span>{String(index + 1).padStart(2, "0")}</span>
                  <strong>{step.label}</strong>
                  <small>{step.value}</small>
                </a>
              ))}
            </nav>
          ) : null}
          <div className={`deploy-workspace-grid ${editorMode === "yaml" ? "yaml-active" : ""}`}>
            <main className="deploy-config-main">
              {editorMode === "form" ? (
                mode === "service"
                  ? <SingleServiceDeployForm lang={lang} draft={serviceDraft} nodes={nodes} storageClasses={storageClasses} onChange={updateServiceDraft} />
                  : <ComposeDeployForm lang={lang} draft={composeDraft} nodes={nodes} storageClasses={storageClasses} onChange={updateComposeDraft} onEditYaml={() => setEditorMode("yaml")} />
              ) : (
                <YamlPreviewEditor
                  mode={mode}
                  serviceYaml={serviceYaml}
                  composeYaml={composeYaml}
                  sidecarYaml={sidecarYaml}
                  onServiceYamlChange={(value) => { setServiceYaml(value); setYamlDirty(true); }}
                  onComposeYamlChange={(value) => { setComposeYaml(value); setYamlDirty(true); }}
                  onSidecarYamlChange={(value) => { setSidecarYaml(value); setYamlDirty(true); }}
                />
              )}
            </main>
            <DeploySummary lang={lang} mode={mode} serviceDraft={serviceDraft} composeDraft={composeDraft} preview={preview} steps={steps} errors={allErrors} />
          </div>
          <div className="deploy-action-bar">
            <div>
              <strong>{yamlDirty ? (lang === "zh" ? "YAML 已手动编辑" : "YAML edited manually") : (lang === "zh" ? "表单同步 YAML" : "Form syncs to YAML")}</strong>
              <span>{lang === "zh" ? <>Secret 使用 ${"{NAME}"} 引用，明文密钥请先存入 Luma Control。</> : <>Secrets must use ${"{NAME}"} references. Store plaintext secrets in Luma Control first.</>}</span>
            </div>
            <button type="button" className="ghost" onClick={() => setEditorMode("yaml")}>
              <FileCode2 size={16} aria-hidden="true" />
              {lang === "zh" ? "预览 YAML" : "Preview YAML"}
            </button>
            <button type="button" className="ghost" disabled={status !== "idle"} onClick={() => void runPreview()}>
              <ListChecks size={16} aria-hidden="true" />
              {status === "previewing" ? (lang === "zh" ? "校验中..." : "Validating...") : (lang === "zh" ? "校验" : "Validate")}
            </button>
            <button type="button" className="primary" disabled={status !== "idle" || validationErrors.length > 0} onClick={() => void runDeploy()}>
              <Rocket size={16} aria-hidden="true" />
              {status === "deploying" ? (lang === "zh" ? "部署中..." : "Deploying...") : (lang === "zh" ? "部署" : "Deploy")}
            </button>
          </div>
        </>
      )}
      </>
      )}
    </section>
  );
}
