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
  const zh = lang === "zh";
  const publicTargets = mode === "service"
    ? serviceDraft.exposure === "none" ? [] : [`${serviceDraft.domain}:${serviceDraft.port}`]
    : composeDraft.services.filter((service) => service.exposure !== "none").map((service) => `${service.name} -> ${service.domain}:${service.port}`);
  const storage = mode === "compose"
    ? composeDraft.volumes.map((volume) => volume.storageMode === "storageClass" ? `${volume.name}:${volume.storageClass || (zh ? "未选择" : "not selected")}` : `${volume.name}:${volume.localNode || "local"}`)
    : (serviceDraft.volumeMounts || []).map((volume) => volume.storageMode === "storageClass" ? `${volume.name}:${volume.storageClass || (zh ? "未选择" : "not selected")}` : `${volume.name}:unmanaged`);
  const previewWarnings = preview ? [...(preview.warnings || []), ...(preview.storage?.warnings || [])] : [];
  return (
    <aside className="deploy-summary">
      <div className="deploy-summary-card primary">
        <p className="eyebrow">{lang === "zh" ? "影响预览" : "Impact"}</p>
        <h3>{mode === "service" ? serviceDraft.name : composeDraft.name}</h3>
        <dl>
          <div><dt>{zh ? "类型" : "Type"}</dt><dd>{mode === "service" ? (zh ? "单服务" : "Single service") : (zh ? "Compose 应用" : "Compose app")}</dd></div>
          <div><dt>{zh ? "调度" : "Placement"}</dt><dd>{mode === "service" ? compact([serviceDraft.region, serviceDraft.node]) : compact([composeDraft.region, `${composeDraft.services.length} services`])}</dd></div>
          <div><dt>{zh ? "入口" : "Ingress"}</dt><dd>{publicTargets.length ? publicTargets.join(", ") : (zh ? "内部服务" : "Internal only")}</dd></div>
          <div><dt>{zh ? "存储" : "Storage"}</dt><dd>{storage.length ? storage.join(", ") : (zh ? "无托管卷" : "No managed volumes")}</dd></div>
        </dl>
      </div>
      <div className="deploy-summary-card warning">
        <h3>{zh ? "部署动作" : "Deploy action"}</h3>
        <p>{zh ? "部署会写入生成的 Nomad job，按配置同步 DNS，并通过 Luma Control 提交到 Nomad。" : "Deploy writes the generated Nomad job, syncs DNS according to the config, and submits it to Nomad through Luma Control."}</p>
      </div>
      {preview ? (
        <div className="deploy-summary-card">
          <h3>{zh ? "预览结果" : "Preview result"}</h3>
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
          <h3>{zh ? "校验错误" : "Validation errors"}</h3>
          {errors.map((error) => <p key={error}>{error}</p>)}
        </div>
      ) : null}
      {steps.length ? (
        <div className="deploy-summary-card deploy-steps">
          <h3>{zh ? "部署步骤" : "Deploy steps"}</h3>
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
