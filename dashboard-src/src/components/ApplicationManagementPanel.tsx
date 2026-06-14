import { useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { fetchDeploymentConfig, type DeploymentConfig } from "../deploymentConfigApi";
import { localizeState, t } from "../i18n";
import { restartApplication } from "../lifecycleApi";
import type { DashboardPayload, Lang } from "../types";
import { groupApplications, serviceRuntimeStatus, type Application } from "./applicationModel";
import { Badge, BadgeGroup, CodeCell, PrimaryCell, StatePill } from "./ui";

export type ApplicationUpdateRequest = {
  app: Application;
  deploymentConfig?: DeploymentConfig;
  configWarning?: string;
};

type ConfigTab = "manifest" | "compose";

const DEPLOY_ROOT = typeof document === "undefined" ? null : document.body;

function accessHref(domain: string) {
  return domain.startsWith("http://") || domain.startsWith("https://") ? domain : `https://${domain}`;
}

function configUpdatedLabel(updatedAt?: number) {
  if (!updatedAt) return "-";
  return new Date(updatedAt * 1000).toLocaleString();
}

export function ApplicationManagementPanel({
  lang,
  token,
  payload,
  onRefresh,
  onCreateApplication,
  onUpdateApplication,
}: {
  lang: Lang;
  token: string;
  payload: DashboardPayload | null;
  onRefresh: () => Promise<void> | void;
  onCreateApplication?: () => void;
  onUpdateApplication?: (request: ApplicationUpdateRequest) => void;
}) {
  const [selected, setSelected] = useState<Application | null>(null);
  const [deploymentConfig, setDeploymentConfig] = useState<DeploymentConfig | null>(null);
  const [deploymentConfigFor, setDeploymentConfigFor] = useState("");
  const [configTab, setConfigTab] = useState<ConfigTab>("manifest");
  const [actionError, setActionError] = useState("");
  const [actionBusy, setActionBusy] = useState("");
  const [configBusy, setConfigBusy] = useState("");
  const applications = useMemo(() => groupApplications(payload?.services || []), [payload?.services]);

  const restart = async (app: Application) => {
    setActionError("");
    if (!window.confirm(lang === "zh" ? `确认重启应用 ${app.stack}？` : `Restart application ${app.stack}?`)) return;
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

  const openCreate = () => {
    if (onCreateApplication) {
      onCreateApplication();
      return;
    }
    setActionError(lang === "zh" ? "当前页面未配置创建应用入口。" : "This page does not have a create-application entry configured.");
  };
  const openDetails = (app: Application) => {
    setDeploymentConfig(null);
    setDeploymentConfigFor("");
    setSelected(app);
  };
  const openUpdate = async (app: Application) => {
    setActionError("");
    if (!onUpdateApplication) {
      setActionError(lang === "zh" ? "当前页面未配置更新应用入口。" : "This page does not have an update-application entry configured.");
      return;
    }
    setConfigBusy(app.stack);
    setSelected(null);
    try {
      const config = await fetchDeploymentConfig({ token, name: app.stack });
      onUpdateApplication({ app, deploymentConfig: config });
    } catch (error) {
      const message = String(error instanceof Error ? error.message : error);
      onUpdateApplication({
        app,
        configWarning: lang === "zh"
          ? `未读取到已登记部署配置，已从当前运行状态反推；提交前请重点核对 YAML。${message ? ` (${message})` : ""}`
          : `Could not load a registered deployment config, so the form was inferred from current runtime state. Review the YAML carefully before submitting.${message ? ` (${message})` : ""}`,
      });
    } finally {
      setConfigBusy("");
    }
  };
  const openConfig = async (app: Application) => {
    setActionError("");
    setConfigBusy(app.stack);
    try {
      const config = await fetchDeploymentConfig({ token, name: app.stack });
      setDeploymentConfig(config);
      setDeploymentConfigFor(app.stack);
      setConfigTab(config.manifest ? "manifest" : "compose");
    } catch (error) {
      setActionError(String(error instanceof Error ? error.message : error));
    } finally {
      setConfigBusy("");
    }
  };

  const selectedDiagnostics = selected?.services.flatMap((service) => service.diagnostics || []) || [];
  const selectedVolumes = selected?.services.flatMap((service) => service.storage || []) || [];
  const selectedConfig = selected && deploymentConfigFor === selected.stack ? deploymentConfig : null;
  const selectedConfigTabs: ConfigTab[] = [
    ...(selectedConfig?.manifest ? ["manifest" as const] : []),
    ...(selectedConfig?.composeContent ? ["compose" as const] : []),
  ];
  const selectedConfigContent = configTab === "compose" ? selectedConfig?.composeContent : selectedConfig?.manifest;
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
            <button type="button" className="ghost" disabled={Boolean(configBusy)} onClick={() => void openConfig(selected)}>{configBusy === selected.stack ? t(lang, "loadingConfig") : t(lang, "viewConfig")}</button>
            <button type="button" className="ghost" disabled={Boolean(actionBusy)} onClick={() => void restart(selected)}>{actionBusy === selected.stack ? t(lang, "restarting") : t(lang, "restart")}</button>
            <button type="button" disabled={Boolean(configBusy)} onClick={() => void openUpdate(selected)}>{configBusy === selected.stack ? t(lang, "loadingConfig") : t(lang, "updateApp")}</button>
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
          {selectedConfig ? (
            <section className="application-detail-section deployment-config-section">
              <div className="deployment-config-heading">
                <div>
                  <h3>{t(lang, "deploymentConfig")}</h3>
                  <span>{t(lang, "source")}: {selectedConfig.sourceName || "-"} · {t(lang, "lastUpdated")}: {configUpdatedLabel(selectedConfig.updatedAt)}</span>
                </div>
                <div className="deployment-config-tabs">
                  {selectedConfigTabs.map((tab) => (
                    <button type="button" className={configTab === tab ? "active" : ""} key={tab} onClick={() => setConfigTab(tab)}>
                      {tab === "compose" ? t(lang, "composeFile") : t(lang, "lumaManifest")}
                    </button>
                  ))}
                </div>
              </div>
              {selectedConfigContent ? (
                <pre className="deployment-config-code"><code>{selectedConfigContent}</code></pre>
              ) : (
                <p className="deployment-config-empty">{t(lang, "noDeploymentConfig")}</p>
              )}
            </section>
          ) : null}
          <section className="application-detail-section">
            <h3>{t(lang, "services")}</h3>
            <div className="application-service-grid">
              {selected.services.map((service) => (
                <article className="application-service-detail" key={service.fullName || service.name}>
                  <div className="application-service-title">
                    <strong>{service.name}</strong>
                    <StatePill label={localizeState(lang, serviceRuntimeStatus(service))} value={serviceRuntimeStatus(service)} />
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
            <h3>{lang === "zh" ? "存储与诊断" : "Storage and diagnostics"}</h3>
            <div className="application-diagnostics-list">
              {selectedVolumes.length ? selectedVolumes.map((volume) => (
                <div key={`${volume.name}-${volume.storageClass}-${volume.node}`}>
                  <strong>{volume.name}</strong>
                  <span>{volume.kind || "unmanaged"} / {volume.storageClass || volume.node || "-"}</span>
                </div>
              )) : <p>{lang === "zh" ? "未发现应用卷" : "No application volumes found"}</p>}
              {selectedDiagnostics.length ? selectedDiagnostics.map((item) => <p key={item}>{item}</p>) : <p>{lang === "zh" ? "暂无诊断告警" : "No diagnostic warnings"}</p>}
            </div>
          </section>
        </div>
      </section>
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
            {applications.length ? applications.map((app) => {
              const openApp = () => openDetails(app);
              return (
              <tr
                aria-label={`${t(lang, "details")}: ${app.stack}`}
                key={app.stack}
                onClick={openApp}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    openApp();
                  }
                }}
                role="button"
                tabIndex={0}
              >
                <td><PrimaryCell title={app.stack} meta={serviceCountLabel(app.services.length)} /></td>
                <td><StatePill label={localizeState(lang, app.status)} value={app.status} /></td>
                <td>
                  {app.domains.length ? <CodeCell value={app.domains.join(", ")} /> : <Badge value={t(lang, "internalOnly")} />}
                </td>
                <td><BadgeGroup>{app.regions.map((region) => <Badge key={region} value={region} />)}</BadgeGroup></td>
                <td><Badge value={`${app.running}/${app.desired}`} /></td>
                <td>
                  <div className="app-action-row">
                    <button type="button" className="ghost" onClick={(event) => { event.stopPropagation(); openDetails(app); }}>{t(lang, "details")}</button>
                    <button type="button" className="ghost" disabled={Boolean(actionBusy)} onClick={(event) => { event.stopPropagation(); void restart(app); }}>{actionBusy === app.stack ? t(lang, "restarting") : t(lang, "restart")}</button>
                    <button type="button" disabled={Boolean(configBusy)} onClick={(event) => { event.stopPropagation(); void openUpdate(app); }}>{configBusy === app.stack ? t(lang, "loadingConfig") : t(lang, "updateApp")}</button>
                  </div>
                </td>
              </tr>
              );
            }) : (
              <tr><td colSpan={6}>{t(lang, "noApplications")}</td></tr>
            )}
          </tbody>
        </table>
      </div>

      {detailOverlay}
    </article>
  );
}
