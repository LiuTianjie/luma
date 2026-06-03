import { useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { localizeState, t } from "../i18n";
import { restartApplication } from "../lifecycleApi";
import type { DashboardPayload, DashboardService, Lang } from "../types";
import { DeployWorkspace } from "../deploy/DeployWorkspace";
import { serviceDraft } from "../deploy/templates";
import type { ComposeDeploymentDraft, ComposeServiceDraft, DeployMode, ServiceManifestDraft } from "../deploy/types";
import { Badge, BadgeGroup, CodeCell, PrimaryCell, StatePill } from "./ui";

type Application = {
  stack: string;
  services: DashboardService[];
  domains: string[];
  status: string;
  running: number;
  desired: number;
  exposure: string;
  regions: string[];
};

type DeployContext = {
  mode: "create" | "update";
  app: Application | null;
  deployMode?: DeployMode;
  serviceDraft?: ServiceManifestDraft;
  composeDraft?: ComposeDeploymentDraft;
};

const SYSTEM_STACKS = new Set(["traefik", "portainer", "egress", "luma-control"]);
const DEPLOY_ROOT = typeof document === "undefined" ? null : document.body;

function isSystemService(service: DashboardService) {
  const stack = service.stack || service.name || "";
  return SYSTEM_STACKS.has(stack) || stack.startsWith("luma-storage") || service.name === "cloudflared";
}

function applicationStatus(services: DashboardService[]) {
  if (services.some((service) => (service.failed || 0) > 0 || service.health === "failed")) return "failed";
  if (services.some((service) => (service.pending || 0) > 0)) return "pending";
  if (services.every((service) => (service.running || 0) >= (service.desired || 0))) return "running";
  return "degraded";
}

function groupApplications(services: DashboardService[]): Application[] {
  const groups = new Map<string, DashboardService[]>();
  for (const service of services) {
    if (isSystemService(service)) continue;
    const stack = service.stack || service.name || "";
    if (!stack) continue;
    groups.set(stack, [...(groups.get(stack) || []), service]);
  }
  return [...groups.entries()].map(([stack, items]) => {
    const domains = items.map((service) => service.domain || "").filter(Boolean);
    const running = items.reduce((sum, service) => sum + (service.running || 0), 0);
    const desired = items.reduce((sum, service) => sum + (service.desired || 0), 0);
    const exposures = [...new Set(items.map((service) => service.exposure || "none"))];
    const regions = [...new Set(items.map((service) => service.region || "-"))];
    return {
      stack,
      services: items,
      domains,
      running,
      desired,
      exposure: exposures.length === 1 ? exposures[0] : "mixed",
      regions,
      status: applicationStatus(items),
    };
  }).sort((a, b) => a.stack.localeCompare(b.stack));
}

function serviceToDraft(app: Application): ServiceManifestDraft {
  const primary = app.services[0] || {};
  return serviceDraft({
    name: app.stack,
    image: primary.image || "",
    region: (primary.region as ServiceManifestDraft["region"]) || "cn",
    node: primary.node || "",
    exposure: (primary.exposure as ServiceManifestDraft["exposure"]) || "none",
    domain: primary.domain || "",
    port: primary.targetPort || "",
    replicas: primary.desired || 1,
    proxy: false,
  });
}

function appToComposeDraft(app: Application): ComposeDeploymentDraft {
  const composeServices: ComposeServiceDraft[] = app.services.map((service) => ({
    name: service.name || "app",
    region: (service.region as ComposeServiceDraft["region"]) || "",
    node: service.node || "",
    exposure: (service.exposure as ComposeServiceDraft["exposure"]) || "none",
    domain: service.domain || "",
    port: service.targetPort || "",
    publishPort: "",
    replicas: service.desired || 1,
    proxy: false,
    env: [],
  }));
  const volumeNames = new Set<string>();
  for (const service of app.services) {
    for (const volume of service.storage || []) {
      if (volume.name) volumeNames.add(volume.name);
    }
  }
  const volumes = [...volumeNames].map((name) => {
    const source = app.services.flatMap((service) => service.storage || []).find((volume) => volume.name === name);
    return {
      name,
      target: "",
      storageMode: source?.storageClass ? "storageClass" as const : "local" as const,
      storageClass: source?.storageClass || "",
      localNode: source?.node || "",
      localPath: source?.networkPath || "",
    };
  });
  const composeYaml = [
    "services:",
    ...app.services.flatMap((service) => [
      `  ${service.name || "app"}:`,
      `    image: ${service.image || "replace-me:latest"}`,
    ]),
    ...(volumes.length ? ["volumes:", ...volumes.map((volume) => `  ${volume.name}: {}`)] : []),
    "",
  ].join("\n");
  return {
    name: app.stack,
    composeFileName: "docker-compose.yml",
    region: (app.regions[0] as ComposeDeploymentDraft["region"]) || "cn",
    services: composeServices,
    volumes,
    dockerComposeYaml: composeYaml,
    skipDns: false,
    skipWebhook: false,
  };
}

function accessHref(domain: string) {
  return domain.startsWith("http://") || domain.startsWith("https://") ? domain : `https://${domain}`;
}

export function ApplicationManagementPanel({
  lang,
  token,
  payload,
  onRefresh,
}: {
  lang: Lang;
  token: string;
  payload: DashboardPayload | null;
  onRefresh: () => Promise<void> | void;
}) {
  const [selected, setSelected] = useState<Application | null>(null);
  const [deployContext, setDeployContext] = useState<DeployContext | null>(null);
  const [actionError, setActionError] = useState("");
  const [actionBusy, setActionBusy] = useState("");
  const applications = useMemo(() => groupApplications(payload?.services || []), [payload?.services]);
  const updateContext = useMemo(() => {
    if (!deployContext) return null;
    if (deployContext.mode !== "update" || !deployContext.app) return deployContext;
    if (deployContext.app.services.length <= 1) {
      return { ...deployContext, deployMode: "service" as const, serviceDraft: serviceToDraft(deployContext.app) };
    }
    return { ...deployContext, deployMode: "compose" as const, composeDraft: appToComposeDraft(deployContext.app) };
  }, [deployContext]);

  const restart = async (app: Application) => {
    setActionError("");
    if (!window.confirm(`确认重启应用 ${app.stack}？`)) return;
    setActionBusy(app.stack);
    try {
      await restartApplication({ token, stack: app.stack });
      await onRefresh();
    } catch (error) {
      setActionError(String(error instanceof Error ? error.message : error));
    } finally {
      setActionBusy("");
    }
  };

  const openCreate = () => setDeployContext({ mode: "create", app: null });
  const openUpdate = (app: Application) => {
    setSelected(null);
    setDeployContext({ mode: "update", app });
  };
  const closeDeploy = () => setDeployContext(null);

  const selectedDiagnostics = selected?.services.flatMap((service) => service.diagnostics || []) || [];
  const selectedVolumes = selected?.services.flatMap((service) => service.storage || []) || [];
  const serviceCountLabel = (count: number) => lang === "zh" ? `${count} 个服务` : `${count} service${count === 1 ? "" : "s"}`;
  const replicaLabel = (running: number, desired: number) => lang === "zh" ? `${running}/${desired} 副本` : `${running}/${desired} replicas`;
  const detailOverlay = selected && DEPLOY_ROOT ? createPortal(
    <div className="application-detail-backdrop" onClick={() => setSelected(null)}>
      <section className="application-detail-page" onClick={(event) => event.stopPropagation()}>
        <header className="application-detail-header">
          <div>
            <p className="eyebrow">{lang === "zh" ? "应用详情" : "Application"}</p>
            <h2>{selected.stack}</h2>
            <span>{serviceCountLabel(selected.services.length)} · {replicaLabel(selected.running, selected.desired)}</span>
          </div>
          <div className="application-detail-actions">
            <button type="button" className="ghost" disabled={Boolean(actionBusy)} onClick={() => void restart(selected)}>{actionBusy === selected.stack ? t(lang, "restarting") : t(lang, "restart")}</button>
            <button type="button" onClick={() => openUpdate(selected)}>{t(lang, "updateApp")}</button>
            <button type="button" className="icon-button" onClick={() => setSelected(null)}>{t(lang, "close")}</button>
          </div>
        </header>
        <div className="application-detail-body">
          <section className="application-overview-grid">
            <article><span>{t(lang, "status")}</span><strong>{localizeState(lang, selected.status)}</strong></article>
            <article><span>{t(lang, "replicas")}</span><strong>{selected.running}/{selected.desired}</strong></article>
            <article><span>{t(lang, "region")}</span><strong>{selected.regions.join(", ")}</strong></article>
            <article><span>{t(lang, "exposure")}</span><strong>{selected.exposure}</strong></article>
          </section>
          <section className="application-detail-section">
            <h3>{t(lang, "accessAddress")}</h3>
            <div className="application-access-list">
              {selected.domains.length ? selected.domains.map((domain) => (
                <a key={domain} href={accessHref(domain)} target="_blank" rel="noreferrer">{domain}</a>
              )) : <p>{t(lang, "internalOnly")}</p>}
            </div>
          </section>
          <section className="application-detail-section">
            <h3>服务</h3>
            <div className="application-service-grid">
              {selected.services.map((service) => (
                <article className="application-service-detail" key={service.fullName || service.name}>
                  <div className="application-service-title">
                    <strong>{service.name}</strong>
                    <StatePill label={localizeState(lang, service.health)} value={service.health} />
                  </div>
                  <dl>
                    <div><dt>{t(lang, "image")}</dt><dd>{service.image || "-"}</dd></div>
                    <div><dt>{t(lang, "accessAddress")}</dt><dd>{service.domain || t(lang, "internalOnly")}</dd></div>
                    <div><dt>{t(lang, "replicas")}</dt><dd>{service.running ?? 0}/{service.desired ?? 0}</dd></div>
                    <div><dt>{t(lang, "nodes")}</dt><dd>{(service.nodes || []).join(", ") || service.node || "-"}</dd></div>
                    <div><dt>{t(lang, "port")}</dt><dd>{service.targetPort || "-"}</dd></div>
                    <div><dt>{t(lang, "network")}</dt><dd>{service.network || "-"}</dd></div>
                  </dl>
                </article>
              ))}
            </div>
          </section>
          <section className="application-detail-section">
            <h3>存储与诊断</h3>
            <div className="application-diagnostics-list">
              {selectedVolumes.length ? selectedVolumes.map((volume) => (
                <div key={`${volume.name}-${volume.storageClass}-${volume.node}`}>
                  <strong>{volume.name}</strong>
                  <span>{volume.kind || "unmanaged"} / {volume.storageClass || volume.node || "-"}</span>
                </div>
              )) : <p>未发现应用卷</p>}
              {selectedDiagnostics.length ? selectedDiagnostics.map((item) => <p key={item}>{item}</p>) : <p>暂无诊断告警</p>}
            </div>
          </section>
        </div>
      </section>
    </div>,
    DEPLOY_ROOT,
  ) : null;

  const deployTitle = deployContext?.mode === "update" && deployContext.app ? `更新应用 · ${deployContext.app.stack}` : t(lang, "createApplication");
  const deploySubtitle = deployContext?.mode === "update" ? "使用同名 stack 部署会更新当前应用，部署前仍会先预览生成结果。" : "选择模板或直接粘贴 YAML，按 Luma 配置创建应用。";
  const updateContextNode = deployContext?.mode === "update" && deployContext.app ? (
    <section className="application-update-context">
      <div className="application-update-context-title">
        <strong>当前应用</strong>
        <span>下面的表单已从现有 stack 带入，提交后会按同名应用更新。</span>
      </div>
      <div className="application-update-context-grid">
        <article><span>Stack</span><strong>{deployContext.app.stack}</strong></article>
        <article><span>服务</span><strong>{deployContext.app.services.length}</strong></article>
        <article><span>{t(lang, "accessAddress")}</span><strong>{deployContext.app.domains.join(", ") || t(lang, "internalOnly")}</strong></article>
        <article><span>{t(lang, "replicas")}</span><strong>{deployContext.app.running}/{deployContext.app.desired}</strong></article>
      </div>
    </section>
  ) : null;

  const deployOverlay = deployContext && DEPLOY_ROOT ? createPortal(
    <div className="deploy-modal-backdrop" onClick={closeDeploy}>
      <div className="deploy-modal" onClick={(event) => event.stopPropagation()}>
        <DeployWorkspace
          lang={lang}
          token={token}
          payload={payload}
          initialMode={updateContext?.deployMode}
          initialServiceDraft={updateContext?.serviceDraft}
          initialComposeDraft={updateContext?.composeDraft}
          contextLabel={deployContext.mode === "update" && deployContext.app ? `更新 ${deployContext.app.stack}` : ""}
          modalTitle={deployTitle}
          modalSubtitle={deploySubtitle}
          modalContext={updateContextNode}
          onClose={closeDeploy}
          onRefresh={async () => { await onRefresh(); closeDeploy(); }}
        />
      </div>
    </div>,
    DEPLOY_ROOT,
  ) : null;

  return (
    <article className="panel app-management-panel" id="section-1">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">{lang === "zh" ? "应用管理" : "Applications"}</p>
          <h2>{t(lang, "applications")}</h2>
        </div>
        <button type="button" onClick={openCreate}>{t(lang, "createApplication")}</button>
      </div>
      {actionError ? <div className="storage-warnings"><span>{actionError}</span></div> : null}
      <div className="table-wrap">
        <table className="app-table">
          <thead>
            <tr>
              <th>{t(lang, "application")}</th>
              <th>{t(lang, "status")}</th>
              <th>{t(lang, "accessAddress")}</th>
              <th>{t(lang, "region")}</th>
              <th>{t(lang, "replicas")}</th>
              <th>{t(lang, "actions")}</th>
            </tr>
          </thead>
          <tbody>
            {applications.length ? applications.map((app) => (
              <tr key={app.stack}>
                <td onClick={() => setSelected(app)}><PrimaryCell title={app.stack} meta={serviceCountLabel(app.services.length)} /></td>
                <td><StatePill label={localizeState(lang, app.status)} value={app.status} /></td>
                <td>
                  {app.domains.length ? <CodeCell value={app.domains.join(", ")} /> : <Badge value={t(lang, "internalOnly")} />}
                </td>
                <td><BadgeGroup>{app.regions.map((region) => <Badge key={region} value={region} />)}</BadgeGroup></td>
                <td><Badge value={`${app.running}/${app.desired}`} /></td>
                <td>
                  <div className="app-action-row">
                    <button type="button" className="ghost" onClick={() => setSelected(app)}>{t(lang, "details")}</button>
                    <button type="button" className="ghost" disabled={Boolean(actionBusy)} onClick={() => void restart(app)}>{actionBusy === app.stack ? t(lang, "restarting") : t(lang, "restart")}</button>
                    <button type="button" onClick={() => openUpdate(app)}>{t(lang, "updateApp")}</button>
                  </div>
                </td>
              </tr>
            )) : (
              <tr><td colSpan={6}>{t(lang, "noApplications")}</td></tr>
            )}
          </tbody>
        </table>
      </div>

      {detailOverlay}
      {deployOverlay}
    </article>
  );
}
