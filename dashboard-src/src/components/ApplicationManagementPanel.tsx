import { useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { FileText, History, Pencil, Plus, RotateCw, Search, Settings2 } from "lucide-react";
import { fetchDeploymentConfig, type DeploymentConfig } from "../deploymentConfigApi";
import { localizeState, t } from "../i18n";
import { fetchServiceHistory, restartApplication, rollbackService } from "../lifecycleApi";
import type { DashboardPayload, DashboardService, Lang, ServiceVersion } from "../types";
import { groupApplications, serviceRuntimeStatus, type Application } from "./applicationModel";
import { ServiceLogsModal } from "./ServiceLogsModal";
import { Badge, BadgeGroup, CodeCell, PrimaryCell, StatePill } from "./ui";

export type ApplicationUpdateRequest = {
  app: Application;
  deploymentConfig?: DeploymentConfig;
  configWarning?: string;
};

type ConfigTab = "manifest" | "compose";

export type ApplicationFilterState = {
  query: string;
  status: string;
  region: string;
};

type RollbackState = {
  app: string;
  versions: ServiceVersion[];
  loading: boolean;
  error: string;
  message: string;
  busyVersion: number | null;
};

type LogsTarget = {
  services: DashboardService[];
  initialServiceName: string;
};

const DEPLOY_ROOT = typeof document === "undefined" ? null : document.body;

function accessHref(domain: string) {
  return domain.startsWith("http://") || domain.startsWith("https://") ? domain : `https://${domain}`;
}

function configUpdatedLabel(updatedAt?: number) {
  if (!updatedAt) return "-";
  return new Date(updatedAt * 1000).toLocaleString();
}

function versionNumber(version: ServiceVersion["version"]) {
  const value = Number(version);
  return Number.isInteger(value) ? value : null;
}

function versionSubmittedLabel(value: ServiceVersion["submitTime"]) {
  const timestamp = Number(value);
  if (!Number.isFinite(timestamp) || timestamp <= 0) return "-";
  let milliseconds = timestamp;
  if (timestamp > 1_000_000_000_000_000_000) {
    milliseconds = timestamp / 1_000_000;
  } else if (timestamp > 1_000_000_000_000_000) {
    milliseconds = timestamp / 1_000;
  } else if (timestamp < 10_000_000_000) {
    milliseconds = timestamp * 1000;
  }
  const date = new Date(milliseconds);
  return Number.isNaN(date.getTime()) ? "-" : date.toLocaleString();
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
  const [rollbackState, setRollbackState] = useState<RollbackState | null>(null);
  const [logsTarget, setLogsTarget] = useState<LogsTarget | null>(null);
  const [filters, setFilters] = useState<ApplicationFilterState>({ query: "", status: "all", region: "all" });
  const applications = useMemo(() => groupApplications(payload?.services || []), [payload?.services]);
  const statusOptions = useMemo(() => [...new Set(applications.map((app) => app.status).filter(Boolean))].sort(), [applications]);
  const regionOptions = useMemo(() => [...new Set(applications.flatMap((app) => app.regions).filter(Boolean))].sort(), [applications]);
  const filteredApplications = useMemo(() => {
    const query = filters.query.trim().toLowerCase();
    return applications.filter((app) => {
      const matchesStatus = filters.status === "all" || app.status === filters.status;
      const matchesRegion = filters.region === "all" || app.regions.includes(filters.region);
      const haystack = [
        app.stack,
        ...app.domains,
        ...app.nodes,
        ...app.services.map((service) => `${service.name || ""} ${service.fullName || ""} ${service.image || ""}`),
      ].join(" ").toLowerCase();
      return matchesStatus && matchesRegion && (!query || haystack.includes(query));
    });
  }, [applications, filters]);

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

  const firstLogService = (app: Application) => app.services.find((service) => service.fullName);

  const openApplicationLogs = (app: Application) => {
    const service = firstLogService(app);
    if (!service?.fullName) {
      setActionError(lang === "zh" ? `应用 ${app.stack} 暂无可读取日志的服务。` : `Application ${app.stack} has no service logs available.`);
      return;
    }
    setActionError("");
    setLogsTarget({ services: app.services, initialServiceName: service.fullName });
  };

  const openServiceLogs = (service: DashboardService, appServices: DashboardService[]) => {
    if (!service.fullName) {
      setActionError(lang === "zh" ? "该服务暂无可读取日志。" : "This service has no logs available.");
      return;
    }
    setActionError("");
    setLogsTarget({ services: appServices, initialServiceName: service.fullName });
  };

  const loadVersions = async (app: Application, message = "") => {
    setActionError("");
    setRollbackState({ app: app.stack, versions: [], loading: true, error: "", message, busyVersion: null });
    try {
      const result = await fetchServiceHistory({ token, name: app.stack });
      setRollbackState({
        app: app.stack,
        versions: result.versions || [],
        loading: false,
        error: "",
        message,
        busyVersion: null,
      });
    } catch (error) {
      setRollbackState({
        app: app.stack,
        versions: [],
        loading: false,
        error: String(error instanceof Error ? error.message : error),
        message: "",
        busyVersion: null,
      });
    }
  };

  const openVersions = async (app: Application) => {
    setDeploymentConfig(null);
    setDeploymentConfigFor("");
    setSelected(app);
    await loadVersions(app);
  };

  const rollbackToVersion = async (app: Application, version: number) => {
    const prompt = lang === "zh"
      ? `确认将 ${app.stack} 的运行态回滚到 v${version}？`
      : `Rollback ${app.stack} runtime to v${version}?`;
    if (!window.confirm(prompt)) return;
    setActionError("");
    setRollbackState((current) => current && current.app === app.stack
      ? { ...current, error: "", message: "", busyVersion: version }
      : current);
    try {
      const result = await rollbackService({ token, name: app.stack, version });
      await onRefresh();
      await loadVersions(app, result.message || (lang === "zh" ? `已回滚到 v${version}` : `Rolled back to v${version}`));
    } catch (error) {
      setRollbackState((current) => current && current.app === app.stack
        ? {
          ...current,
          loading: false,
          error: String(error instanceof Error ? error.message : error),
          message: "",
          busyVersion: null,
        }
        : current);
    }
  };

  const selectedDiagnostics = selected?.services.flatMap((service) => service.diagnostics || []) || [];
  const selectedVolumes = selected?.services.flatMap((service) => service.storage || []) || [];
  const selectedConfig = selected && deploymentConfigFor === selected.stack ? deploymentConfig : null;
  const selectedRollback = selected && rollbackState?.app === selected.stack ? rollbackState : null;
  const selectedRollbackBusy = selectedRollback?.busyVersion !== null && selectedRollback?.busyVersion !== undefined;
  const selectedConfigTabs: ConfigTab[] = [
    ...(selectedConfig?.manifest ? ["manifest" as const] : []),
    ...(selectedConfig?.composeContent ? ["compose" as const] : []),
  ];
  const selectedConfigContent = configTab === "compose" ? selectedConfig?.composeContent : selectedConfig?.manifest;
  const serviceCountLabel = (count: number) => lang === "zh" ? `${count} 个服务` : `${count} service${count === 1 ? "" : "s"}`;
  const replicaLabel = (running: number, desired: number) => lang === "zh" ? `${running}/${desired} 副本` : `${running}/${desired} replicas`;
  const logLabel = lang === "zh" ? "日志" : "Logs";
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
            <button type="button" className="ghost" disabled={Boolean(selectedRollback?.loading || selectedRollbackBusy)} onClick={() => void openVersions(selected)}>{selectedRollback?.loading ? t(lang, "loadingHistory") : t(lang, "versions")}</button>
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
            <article><span>{t(lang, "nodes")}</span><strong>{selected.nodes.join(", ") || "-"}</strong></article>
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
          {selectedRollback ? (
            <section className="application-detail-section version-history-section">
              <div className="version-history-heading">
                <h3>{t(lang, "versions")}</h3>
                <button type="button" className="ghost" disabled={selectedRollback.loading || selectedRollback.busyVersion !== null} onClick={() => void loadVersions(selected)}>{selectedRollback.loading ? t(lang, "loadingHistory") : t(lang, "refresh")}</button>
              </div>
              {selectedRollback.message ? <div className="rollback-message">{selectedRollback.message}</div> : null}
              {selectedRollback.error ? <div className="storage-warnings"><span>{selectedRollback.error}</span></div> : null}
              {selectedRollback.loading ? (
                <p className="deployment-config-empty">{t(lang, "loadingHistory")}</p>
              ) : selectedRollback.versions.length ? (
                <div className="version-history-list">
                  {selectedRollback.versions.map((version, index) => {
                    const targetVersion = versionNumber(version.version);
                    const isCurrent = index === 0;
                    const isBusy = targetVersion !== null && selectedRollback.busyVersion === targetVersion;
                    return (
                      <div className={isCurrent ? "version-history-row current" : "version-history-row"} key={`${version.version ?? "unknown"}-${index}`}>
                        <div className="version-history-main">
                          <strong>v{version.version ?? "-"}</strong>
                          <CodeCell value={version.image || "-"} />
                        </div>
                        <div className="version-history-meta">
                          <Badge value={version.stable ? t(lang, "stableVersion") : "-"} />
                          <span>{t(lang, "submitted")}: {versionSubmittedLabel(version.submitTime)}</span>
                        </div>
                        <div className="version-history-action">
                          {isCurrent ? (
                            <Badge value={t(lang, "currentVersion")} />
                          ) : targetVersion === null ? (
                            <Badge value="-" />
                          ) : (
                            <button type="button" className="ghost" disabled={selectedRollback.busyVersion !== null} onClick={() => void rollbackToVersion(selected, targetVersion)}>
                              {isBusy ? t(lang, "rollingBack") : t(lang, "rollbackToVersion")}
                            </button>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              ) : (
                <p className="deployment-config-empty">{t(lang, "noVersionHistory")}</p>
              )}
            </section>
          ) : null}
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
                    <div className="application-service-title-actions">
                      <StatePill label={localizeState(lang, serviceRuntimeStatus(service))} value={serviceRuntimeStatus(service)} />
                      <button
                        type="button"
                        className="ghost service-log-button"
                        disabled={!service.fullName}
                        onClick={() => openServiceLogs(service, selected.services)}
                      >
                        <FileText size={15} aria-hidden="true" />
                        {logLabel}
                      </button>
                    </div>
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
  const logsOverlay = logsTarget ? (
    <ServiceLogsModal
      lang={lang}
      token={token}
      services={logsTarget.services}
      initialServiceName={logsTarget.initialServiceName}
      onClose={() => setLogsTarget(null)}
    />
  ) : null;

  return (
    <article className="panel app-management-panel" id="section-1">
      <div className="panel-heading app-management-heading">
        <div>
          <p className="eyebrow">{lang === "zh" ? "应用管理" : "Applications"}</p>
          <h2>{t(lang, "applications")}</h2>
        </div>
        <button type="button" onClick={openCreate}>
          <Plus size={16} aria-hidden="true" />
          {t(lang, "createApplication")}
        </button>
      </div>
      {actionError ? <div className="storage-warnings"><span>{actionError}</span></div> : null}
      <div className="application-filter-bar" aria-label={lang === "zh" ? "应用筛选" : "Application filters"}>
        <label className="application-search-field">
          <Search size={16} aria-hidden="true" />
          <span className="sr-only">{lang === "zh" ? "搜索应用" : "Search applications"}</span>
          <input
            value={filters.query}
            onChange={(event) => setFilters((current) => ({ ...current, query: event.target.value }))}
            placeholder={lang === "zh" ? "搜索应用、域名、镜像" : "Search app, domain, image"}
          />
        </label>
        <label>
          <span>{t(lang, "status")}</span>
          <select value={filters.status} onChange={(event) => setFilters((current) => ({ ...current, status: event.target.value }))}>
            <option value="all">{lang === "zh" ? "全部状态" : "All statuses"}</option>
            {statusOptions.map((status) => <option value={status} key={status}>{localizeState(lang, status)}</option>)}
          </select>
        </label>
        <label>
          <span>{t(lang, "region")}</span>
          <select value={filters.region} onChange={(event) => setFilters((current) => ({ ...current, region: event.target.value }))}>
            <option value="all">{lang === "zh" ? "全部区域" : "All regions"}</option>
            {regionOptions.map((region) => <option value={region} key={region}>{region}</option>)}
          </select>
        </label>
        <div className="application-filter-count">
          <strong>{filteredApplications.length}</strong>
          <span>{lang === "zh" ? ` / ${applications.length} 个应用` : ` / ${applications.length} apps`}</span>
        </div>
      </div>
      <div className="table-wrap">
        <table className="app-table">
          <thead>
            <tr>
              <th>{t(lang, "application")}</th>
              <th>{t(lang, "status")}</th>
              <th>{t(lang, "accessAddress")}</th>
              <th>{t(lang, "region")}</th>
              <th>{t(lang, "nodes")}</th>
              <th>{t(lang, "replicas")}</th>
              <th>{t(lang, "actions")}</th>
            </tr>
          </thead>
          <tbody>
            {filteredApplications.length ? filteredApplications.map((app) => {
              const openApp = () => openDetails(app);
              const hasLogs = Boolean(firstLogService(app));
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
                <td>
                  {app.nodes.length ? (
                    <BadgeGroup>{app.nodes.map((node) => <Badge key={node} value={node} />)}</BadgeGroup>
                  ) : (
                    <Badge value="-" />
                  )}
                </td>
                <td><Badge value={`${app.running}/${app.desired}`} /></td>
                <td>
                  <div className="app-action-row">
                    <button
                      type="button"
                      className="ghost app-log-action"
                      disabled={!hasLogs}
                      onClick={(event) => { event.stopPropagation(); openApplicationLogs(app); }}
                    >
                      <FileText size={15} aria-hidden="true" />
                      {logLabel}
                    </button>
                    <button type="button" className="ghost" onClick={(event) => { event.stopPropagation(); openDetails(app); }}>
                      <Settings2 size={15} aria-hidden="true" />
                      {t(lang, "details")}
                    </button>
                    <button type="button" className="ghost" disabled={rollbackState?.app === app.stack && rollbackState.loading} onClick={(event) => { event.stopPropagation(); void openVersions(app); }}>
                      <History size={15} aria-hidden="true" />
                      {rollbackState?.app === app.stack && rollbackState.loading ? t(lang, "loadingHistory") : t(lang, "versions")}
                    </button>
                    <button type="button" className="ghost" disabled={Boolean(actionBusy)} onClick={(event) => { event.stopPropagation(); void restart(app); }}>
                      <RotateCw size={15} aria-hidden="true" />
                      {actionBusy === app.stack ? t(lang, "restarting") : t(lang, "restart")}
                    </button>
                    <button type="button" disabled={Boolean(configBusy)} onClick={(event) => { event.stopPropagation(); void openUpdate(app); }}>
                      <Pencil size={15} aria-hidden="true" />
                      {configBusy === app.stack ? t(lang, "loadingConfig") : t(lang, "updateApp")}
                    </button>
                  </div>
                </td>
              </tr>
              );
            }) : (
              <tr><td colSpan={7}>{t(lang, "noApplications")}</td></tr>
            )}
          </tbody>
        </table>
      </div>
      <div className="application-card-list">
        {filteredApplications.length ? filteredApplications.map((app) => (
          <article className="application-mobile-card" key={app.stack}>
            <header>
              <PrimaryCell title={app.stack} meta={serviceCountLabel(app.services.length)} />
              <StatePill label={localizeState(lang, app.status)} value={app.status} />
            </header>
            <dl>
              <div><dt>{t(lang, "accessAddress")}</dt><dd>{app.domains.length ? app.domains.join(", ") : t(lang, "internalOnly")}</dd></div>
              <div><dt>{t(lang, "region")}</dt><dd>{app.regions.join(", ") || "-"}</dd></div>
              <div><dt>{t(lang, "nodes")}</dt><dd>{app.nodes.join(", ") || "-"}</dd></div>
              <div><dt>{t(lang, "replicas")}</dt><dd>{app.running}/{app.desired}</dd></div>
            </dl>
            <div className="app-card-actions">
              <button type="button" className="ghost app-log-action" disabled={!firstLogService(app)} onClick={() => openApplicationLogs(app)}>
                <FileText size={15} aria-hidden="true" />
                {logLabel}
              </button>
              <button type="button" className="ghost" onClick={() => openDetails(app)}>
                <Settings2 size={15} aria-hidden="true" />
                {t(lang, "details")}
              </button>
              <button type="button" className="ghost" disabled={rollbackState?.app === app.stack && rollbackState.loading} onClick={() => void openVersions(app)}>
                <History size={15} aria-hidden="true" />
                {rollbackState?.app === app.stack && rollbackState.loading ? t(lang, "loadingHistory") : t(lang, "versions")}
              </button>
              <button type="button" className="ghost" disabled={Boolean(actionBusy)} onClick={() => void restart(app)}>
                <RotateCw size={15} aria-hidden="true" />
                {actionBusy === app.stack ? t(lang, "restarting") : t(lang, "restart")}
              </button>
              <button type="button" disabled={Boolean(configBusy)} onClick={() => void openUpdate(app)}>
                <Pencil size={15} aria-hidden="true" />
                {configBusy === app.stack ? t(lang, "loadingConfig") : t(lang, "updateApp")}
              </button>
            </div>
          </article>
        )) : <div className="empty-inline">{t(lang, "noApplications")}</div>}
      </div>

      {detailOverlay}
      {logsOverlay}
    </article>
  );
}
