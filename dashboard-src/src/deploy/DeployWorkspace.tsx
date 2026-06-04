import { useEffect, useMemo, useState, type ReactNode } from "react";
import type { DashboardPayload, Lang } from "../types";
import { ComposeDeployForm } from "./ComposeDeployForm";
import { deployStream, previewCompose, previewService } from "./deployApi";
import { DeploySummary } from "./DeploySummary";
import { DeployTemplates } from "./DeployTemplates";
import { DEPLOY_TEMPLATES } from "./templates";
import type { ComposeDeploymentDraft, DeployMode, DeployPreviewResult, DeployStep, DeployTemplate, ServiceManifestDraft } from "./types";
import { composeDraftToSidecarYaml, serviceDraftToYaml, syncComposeYamlWithDraft } from "./yaml";
import { SingleServiceDeployForm } from "./SingleServiceDeployForm";
import { YamlPreviewEditor } from "./YamlPreviewEditor";

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function firstTemplate(mode: DeployMode) {
  return DEPLOY_TEMPLATES.find((template) => template.mode === mode) || DEPLOY_TEMPLATES[0];
}

const secretRefPattern = /^\$\{[A-Za-z_][A-Za-z0-9_]*\}$/;

function isPositiveInteger(value: string | number) {
  const numeric = Number(value);
  return Number.isInteger(numeric) && numeric > 0;
}

function exposureRegionError(exposure: ServiceManifestDraft["exposure"], region: ServiceManifestDraft["region"] | "", label: string) {
  if (exposure === "cn-edge" && region !== "cn") return `${label} exposure=cn-edge 必须使用 region=cn`;
  if (exposure === "external-edge" && region !== "global") return `${label} exposure=external-edge 必须使用 region=global`;
  if (exposure === "tailscale-relay" && region !== "home") return `${label} exposure=tailscale-relay 必须使用 region=home`;
  return "";
}

function serviceErrors(draft: ServiceManifestDraft, yamlDirty: boolean, serviceYaml: string) {
  if (yamlDirty) return serviceYaml.trim() ? [] : ["service.yaml 不能为空"];
  const errors = [];
  if (!draft.name.trim()) errors.push("服务名不能为空");
  if (!draft.image.trim()) errors.push("镜像不能为空");
  const regionError = exposureRegionError(draft.exposure, draft.region, draft.name || "服务");
  if (regionError) errors.push(regionError);
  if (draft.exposure !== "none" && !draft.domain.trim()) errors.push("公开入口必须填写域名");
  if (draft.exposure !== "none" && !draft.port.trim()) errors.push("公开入口必须填写容器端口");
  if (draft.exposure !== "none" && draft.port.trim() && !isPositiveInteger(draft.port)) errors.push("容器端口必须是正整数");
  if (draft.publishPort.trim() && !isPositiveInteger(draft.publishPort)) errors.push("发布端口必须是正整数");
  if (!isPositiveInteger(draft.replicas)) errors.push("副本数必须是大于 0 的整数");
  for (const row of draft.env || []) {
    if (!row.key.trim() && row.value.trim()) errors.push("环境变量缺少名称");
    if (row.kind === "secret" && row.key.trim() && !secretRefPattern.test(row.value.trim())) {
      errors.push(`${row.key.trim()} 密钥值必须使用 \${NAME} 引用`);
    }
  }
  return errors;
}

function composeErrors(draft: ComposeDeploymentDraft, yamlDirty: boolean, composeYaml: string, sidecarYaml: string) {
  if (yamlDirty) {
    const errors = [];
    if (!composeYaml.trim()) errors.push("docker-compose.yml 不能为空");
    if (!sidecarYaml.trim()) errors.push("luma.compose.yml 不能为空");
    return errors;
  }
  const errors = [];
  if (!draft.name.trim()) errors.push("应用名不能为空");
  for (const service of draft.services) {
    const region = service.region || draft.region;
    const regionError = exposureRegionError(service.exposure, region, service.name);
    if (regionError) errors.push(regionError);
    if (service.exposure !== "none" && !service.domain.trim()) errors.push(`${service.name} 必须填写域名`);
    if (service.exposure !== "none" && !service.port.trim()) errors.push(`${service.name} 必须填写容器端口`);
    if (service.exposure !== "none" && service.port.trim() && !isPositiveInteger(service.port)) errors.push(`${service.name} 容器端口必须是正整数`);
    if (service.publishPort.trim() && !isPositiveInteger(service.publishPort)) errors.push(`${service.name} 发布端口必须是正整数`);
    if (!isPositiveInteger(service.replicas)) errors.push(`${service.name} 副本数必须是大于 0 的整数`);
    for (const row of service.env || []) {
      if (!row.key.trim() && row.value.trim()) errors.push(`${service.name} 环境变量缺少名称`);
      if (row.kind === "secret" && row.key.trim() && !secretRefPattern.test(row.value.trim())) {
        errors.push(`${service.name}.${row.key.trim()} 密钥值必须使用 \${NAME} 引用`);
      }
    }
  }
  for (const volume of draft.volumes) {
    if (volume.storageMode === "storageClass" && !volume.storageClass) errors.push(`${volume.name} 必须选择 storageClass`);
    if (volume.storageMode === "local" && (!volume.localNode || !volume.localPath)) errors.push(`${volume.name} 必须填写本地节点和路径`);
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
  onClose,
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
  onClose?: () => void;
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
  const [yamlDirty, setYamlDirty] = useState(Boolean(initialYamlDirty));
  const [preview, setPreview] = useState<DeployPreviewResult | null>(null);
  const [steps, setSteps] = useState<DeployStep[]>([]);
  const [status, setStatus] = useState<"idle" | "previewing" | "deploying">("idle");
  const [runtimeErrors, setRuntimeErrors] = useState<string[]>([]);
  const nodes = payload?.nodes || [];
  const storageClasses = payload?.storage?.storageClasses || [];

  useEffect(() => {
    if (!initialMode && !initialServiceDraft && !initialComposeDraft && !initialServiceYaml && !initialSidecarYaml && !initialComposeYaml) return;
    setMode(initialMode || "service");
    setActiveTemplateId("current-application");
    setPreview(null);
    setSteps([]);
    setRuntimeErrors([]);
    setYamlDirty(Boolean(initialYamlDirty));
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
      ? serviceErrors(serviceDraft, yamlDirty, serviceYaml)
      : composeErrors(composeDraft, yamlDirty, composeYaml, sidecarYaml),
    [composeDraft, composeYaml, mode, serviceDraft, serviceYaml, sidecarYaml, yamlDirty],
  );
  const allErrors = [...validationErrors, ...runtimeErrors];

  const selectTemplate = (template: DeployTemplate) => {
    setActiveTemplateId(template.id);
    setMode(template.mode);
    setPreview(null);
    setSteps([]);
    setRuntimeErrors([]);
    setYamlDirty(false);
    setSourceName(template.mode === "compose" ? "luma.compose.yml" : "service.yaml");
    if (template.mode === "service" && template.service) {
      const next = clone(template.service);
      setServiceDraft(next);
      setServiceYaml(serviceDraftToYaml(next));
    }
    if (template.mode === "compose" && template.compose) {
      const next = clone(template.compose);
      setComposeDraft(next);
      setComposeYaml(next.dockerComposeYaml);
      setSidecarYaml(composeDraftToSidecarYaml(next));
    }
  };

  const changeMode = (nextMode: DeployMode) => {
    const template = firstTemplate(nextMode);
    selectTemplate(template);
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
        ? await previewService({ token, manifest: serviceYaml, sourceName, skipDns: serviceDraft.skipDns, skipWebhook: serviceDraft.skipWebhook })
        : await previewCompose({ token, manifest: sidecarYaml, composeContent: composeYaml, sourceName, skipDns: composeDraft.skipDns, skipWebhook: composeDraft.skipWebhook });
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
    if (!window.confirm(`确认部署 ${target} 到当前 Luma Control 集群？`)) return;
    setStatus("deploying");
    try {
      await deployStream(
        mode === "service"
          ? { token, manifest: serviceYaml, sourceName, skipDns: serviceDraft.skipDns, skipWebhook: serviceDraft.skipWebhook }
          : { token, manifest: sidecarYaml, composeContent: composeYaml, sourceName, skipDns: composeDraft.skipDns, skipWebhook: composeDraft.skipWebhook },
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
    <section className={`deploy-workspace-panel ${modalTitle ? "modal-deploy-workspace" : ""}`} id="section-6">
      <div className="panel-heading deploy-heading">
        <div>
          <h2>{modalTitle || (lang === "zh" ? "部署工作台" : "Deploy Workspace")}</h2>
          {modalSubtitle || contextLabel ? <small className="deploy-context-label">{modalSubtitle || contextLabel}</small> : null}
        </div>
        <div className="deploy-heading-actions">
          <div className="deploy-editor-tabs">
            <button type="button" className={editorMode === "form" ? "active" : ""} onClick={() => setEditorMode("form")}>配置表单</button>
            <button type="button" className={editorMode === "yaml" ? "active" : ""} onClick={() => setEditorMode("yaml")}>YAML 文件</button>
          </div>
          {onClose ? <button type="button" className="icon-button" onClick={onClose}>{lang === "zh" ? "关闭" : "Close"}</button> : null}
        </div>
      </div>
      {modalContext}
      <DeployTemplates lang={lang} mode={mode} templates={DEPLOY_TEMPLATES} activeId={activeTemplateId} onModeChange={changeMode} onSelect={selectTemplate} />
      <div className="deploy-workspace-grid">
        <main className="deploy-config-main">
          {editorMode === "form" ? (
            mode === "service"
              ? <SingleServiceDeployForm draft={serviceDraft} nodes={nodes} onChange={updateServiceDraft} />
              : <ComposeDeployForm draft={composeDraft} nodes={nodes} storageClasses={storageClasses} onChange={updateComposeDraft} onEditYaml={() => setEditorMode("yaml")} />
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
          <strong>{yamlDirty ? "YAML 已手动编辑" : "表单同步 YAML"}</strong>
          <span>Secret 使用 ${"{NAME}"} 引用，明文密钥请先存入 Luma Control。</span>
        </div>
        <button type="button" className="ghost" onClick={() => setEditorMode("yaml")}>预览 YAML</button>
        <button type="button" className="ghost" disabled={status !== "idle"} onClick={() => void runPreview()}>{status === "previewing" ? "校验中..." : "校验"}</button>
        <button type="button" disabled={status !== "idle" || validationErrors.length > 0} onClick={() => void runDeploy()}>{status === "deploying" ? "部署中..." : "部署"}</button>
      </div>
    </section>
  );
}
