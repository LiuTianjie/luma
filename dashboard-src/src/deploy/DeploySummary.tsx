import type { Lang } from "../types";
import type { ComposeDeploymentDraft, DeployMode, DeployPreviewResult, DeployStep, ServiceManifestDraft } from "./types";

function compact(values: Array<string | number | undefined | null | false>) {
  return values.filter((value) => value !== undefined && value !== null && value !== false && value !== "").join(" / ") || "-";
}

export function DeploySummary({
  lang,
  mode,
  serviceDraft,
  composeDraft,
  preview,
  steps,
  errors,
}: {
  lang: Lang;
  mode: DeployMode;
  serviceDraft: ServiceManifestDraft;
  composeDraft: ComposeDeploymentDraft;
  preview: DeployPreviewResult | null;
  steps: DeployStep[];
  errors: string[];
}) {
  const publicTargets = mode === "service"
    ? serviceDraft.exposure === "none" ? [] : [`${serviceDraft.domain}:${serviceDraft.port}`]
    : composeDraft.services.filter((service) => service.exposure !== "none").map((service) => `${service.name} -> ${service.domain}:${service.port}`);
  const storage = mode === "compose"
    ? composeDraft.volumes.map((volume) => volume.storageMode === "storageClass" ? `${volume.name}:${volume.storageClass || "未选择"}` : `${volume.name}:${volume.localNode || "local"}`)
    : [];
  const previewWarnings = preview ? [...(preview.warnings || []), ...(preview.storage?.warnings || [])] : [];
  return (
    <aside className="deploy-summary">
      <div className="deploy-summary-card primary">
        <p className="eyebrow">{lang === "zh" ? "影响预览" : "Impact"}</p>
        <h3>{mode === "service" ? serviceDraft.name : composeDraft.name}</h3>
        <dl>
          <div><dt>类型</dt><dd>{mode === "service" ? "单服务" : "Compose 应用"}</dd></div>
          <div><dt>调度</dt><dd>{mode === "service" ? compact([serviceDraft.region, serviceDraft.node]) : compact([composeDraft.region, `${composeDraft.services.length} services`])}</dd></div>
          <div><dt>入口</dt><dd>{publicTargets.length ? publicTargets.join(", ") : "内部服务"}</dd></div>
          <div><dt>存储</dt><dd>{storage.length ? storage.join(", ") : "无托管卷"}</dd></div>
        </dl>
      </div>
      <div className="deploy-summary-card warning">
        <h3>部署动作</h3>
        <p>部署会写入生成的 stack，按配置同步 DNS，并通过 Portainer 更新 Swarm 服务。</p>
      </div>
      {preview ? (
        <div className="deploy-summary-card">
          <h3>预览结果</h3>
          <dl>
            <div><dt>Artifacts</dt><dd>{preview.artifacts?.length || 0}</dd></div>
            <div><dt>Warnings</dt><dd>{previewWarnings.length}</dd></div>
          </dl>
          {preview.artifacts?.length ? preview.artifacts.map((artifact) => (
            <p key={`${artifact.kind}-${artifact.path}`}>{artifact.kind}: {artifact.path}</p>
          )) : null}
          {previewWarnings.map((warning, index) => <p key={`${warning}-${index}`}>{warning}</p>)}
        </div>
      ) : null}
      {errors.length ? (
        <div className="deploy-summary-card errors">
          <h3>校验错误</h3>
          {errors.map((error) => <p key={error}>{error}</p>)}
        </div>
      ) : null}
      {steps.length ? (
        <div className="deploy-summary-card deploy-steps">
          <h3>部署步骤</h3>
          {steps.map((step, index) => (
            <div className={`deploy-step ${step.status || ""}`} key={`${step.name || step.status}-${index}`}>
              <span>{step.status || "-"}</span>
              <strong>{step.name || (step.status === "done" ? "Done" : "Event")}</strong>
              <small>{step.message || ""}</small>
            </div>
          ))}
        </div>
      ) : null}
    </aside>
  );
}
