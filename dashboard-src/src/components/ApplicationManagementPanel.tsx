import { ApplicationProperties, ApplicationVersionEntry } from "./ApplicationProperties";
import "./ApplicationManagementPanel.css";
import { useEffect, useMemo, useRef, useState } from "react";
import { FileText, History, Loader2, MoreHorizontal, Pencil, RotateCw, Search, Settings2, SquareTerminal } from "lucide-react";
import { fetchDeploymentConfig, type DeploymentConfig } from "../deploymentConfigApi";
import { localizeState, t } from "../i18n";
import { fetchServiceHistory, restartApplication, rollbackService, updateApplicationStream } from "../lifecycleApi";
import { formatTimestamp } from "../format";
import type { DeployStep } from "../deploy/types";
import type { DashboardPayload, DashboardService, Lang, ServiceVersion } from "../types";
import { groupApplications, serviceRuntimeStatus, type Application } from "./applicationModel";
import { applicationEndpoints } from "./applicationEndpoints";
import { ServiceLogsModal } from "./ServiceLogsModal";
import { useRouter, toHref } from "../router";
import { StepLog } from "../deploy/StepLog";
import { ObservabilityPanel } from "./ObservabilityPanel";
import { applicationPath, parseApplicationPath, APPLICATION_TABS } from "./applicationRoutes";
import { useConfirm } from "./ConfirmDialog";
import { Badge, BadgeGroup, CodeCell, PrimaryCell, SelectControl, StatePill } from "./ui";

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
  onUpdateApplication,
  onNavigateToDeployments,
  onServiceTerminal,
  selectedStack,
  onSelectApplication,
}: {
  lang: Lang;
  token: string;
  payload: DashboardPayload | null;
  onRefresh: () => Promise<void> | void;
  onUpdateApplication?: (request: ApplicationUpdateRequest) => void;
  onNavigateToDeployments?: () => void;
  onServiceTerminal?: (service: DashboardService, stack: string) => void;
  selectedStack?: string | null;
  onSelectApplication: (stack: string | null) => void;
}) {
  const { path, search, navigate } = useRouter();
  const route = parseApplicationPath(path);
  const tab = route.tab;
  const { confirm, element: confirmDialog } = useConfirm(lang);
  const applications = useMemo(() => groupApplications(payload?.services || []), [payload?.services]);
  // Resolve against each fresh snapshot; URL state drives selection and browser back.
  const selected = applications.find((app) => app.stack === selectedStack) || null;
  const setSelected = (app: Application | null) => onSelectApplication(app?.stack || null);
  const configRequest = useRef(0);
  const versionsRequest = useRef(0);
  const [detailRefresh, setDetailRefresh] = useState(0);
  useEffect(() => {
    const refresh = () => setDetailRefresh((current) => current + 1);
    window.addEventListener("luma:refresh", refresh);
    return () => window.removeEventListener("luma:refresh", refresh);
  }, []);
  const [deploymentConfig, setDeploymentConfig] = useState<DeploymentConfig | null>(null);
  const [deploymentConfigFor, setDeploymentConfigFor] = useState("");
  const [configCopyNotice, setConfigCopyNotice] = useState("");
  const [configTab, setConfigTab] = useState<ConfigTab>("manifest");
  const [actionError, setActionError] = useState("");
  const [actionNotice, setActionNotice] = useState("");
  const [actionBusy, setActionBusy] = useState("");
  const [updatingApp, setUpdatingApp] = useState("");
  const [actionSteps, setActionSteps] = useState<DeployStep[]>([]);
  const [configBusy, setConfigBusy] = useState("");
  const [rollbackState, setRollbackState] = useState<RollbackState | null>(null);
  const filters = useMemo<ApplicationFilterState>(() => {
    const params = new URLSearchParams(search);
    return { query: params.get("q") || "", status: params.get("status") || "all", region: params.get("region") || "all" };
  }, [search]);
  const setFilters = (update: (current: ApplicationFilterState) => ApplicationFilterState) => {
    const next = update(filters);
    const params = new URLSearchParams(search);
    for (const [key, value] of [["q", next.query], ["status", next.status], ["region", next.region]]) {
      if (value && value !== "all") params.set(key, value);
      else params.delete(key);
    }
    navigate(`${path}${params.size ? `?${params}` : ""}`, { replace: true });
  };
  const [openMenu, setOpenMenu] = useState<string | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
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

  useEffect(() => {
    if (!openMenu) return;
    const close = (event: PointerEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) setOpenMenu(null);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpenMenu(null);
    };
    window.addEventListener("pointerdown", close);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("pointerdown", close);
      window.removeEventListener("keydown", onKey);
    };
  }, [openMenu]);

  const restart = async (app: Application) => {
    setActionError("");
    setActionNotice("");
    const ok = await confirm({
      title: lang === "zh" ? `重启 ${app.stack}？` : `Restart ${app.stack}?`,
      body: lang === "zh"
        ? <p>当前运行实例会被销毁并重建，重建期间该应用短暂不可用。配置和数据卷不受影响。</p>
        : <p>The running allocation is destroyed and recreated, so the application is briefly unavailable. Configuration and volumes are untouched.</p>,
      confirmLabel: lang === "zh" ? "重启" : "Restart",
      warning: lang === "zh"
        ? `影响 ${app.services.length} 个服务 · ${app.running}/${app.desired} 副本`
        : `Affects ${app.services.length} service(s) · ${app.running}/${app.desired} replicas`,
    });
    if (!ok) return;
    setActionBusy(app.stack);
    try {
      const result = await restartApplication({ token, stack: app.stack });
      const replacements = result.replacementAllocations || [];
      if (result.mode !== "recreate" || replacements.length === 0) {
        throw new Error(lang === "zh" ? "控制面未返回新的运行实例，重启未完成。" : "Control did not return a replacement allocation; restart did not complete.");
      }
      const shortIds = replacements.map((id) => id.slice(0, 8)).join(", ");
      setActionNotice(lang === "zh" ? `应用已重建，新实例：${shortIds}` : `Application recreated. New allocation: ${shortIds}`);
      await onRefresh();
    } catch (error) {
      setActionError(String(error instanceof Error ? error.message : error));
    } finally {
      setActionBusy("");
    }
  };

  const openDetails = (app: Application) => {
    setDeploymentConfig(null);
    setDeploymentConfigFor("");
    setSelected(app);
  };
  const openUpdate = async (app: Application) => {
    setActionError("");
    setActionSteps([]);
    setConfigBusy(app.stack);
    setSelected(app);
    let gitUpdateStarted = false;
    try {
      const config = await fetchDeploymentConfig({ token, name: app.stack });
      if (config.gitSource) {
        const source = config.gitSource.repository || config.gitSource.repoUrl || config.sourceName || app.stack;
        const ref = config.gitSource.ref;
        const ok = await confirm({
          title: lang === "zh" ? `从 Git 更新 ${app.stack}？` : `Update ${app.stack} from Git?`,
          tone: "neutral",
          body: (
            <>
              <p>{lang === "zh"
                ? "会重新拉取仓库、构建镜像并部署，构建进度在当前应用页面显示，并可在交付记录追溯。"
                : "Re-clones the repository, builds a new image and deploys it. Progress remains visible on this application page and in delivery history."}</p>
              <p><code>{source}{ref ? ` @ ${ref}` : ""}</code></p>
            </>
          ),
          confirmLabel: lang === "zh" ? "开始更新" : "Start update",
        });
        if (!ok) return;
        setConfigBusy("");
        gitUpdateStarted = true;
        setUpdatingApp(app.stack);
        navigate(applicationPath(app.stack, "overview"));
        let failed = "";
        await updateApplicationStream({ token, name: app.stack }, (step) => {
          setActionSteps((current) => [...current, step]);
          if (step.status === "fail") failed = step.message || step.name || "Update failed";
        });
        if (failed) throw new Error(failed);
        setActionNotice(lang === "zh" ? `${app.stack} 更新流程已完成` : `${app.stack} update completed`);
        await onRefresh();
        return;
      }
      if (!onUpdateApplication) {
        setActionError(lang === "zh" ? "当前页面未配置更新应用入口。" : "This page does not have an update-application entry configured.");
        return;
      }
      onUpdateApplication({ app, deploymentConfig: config });
    } catch (error) {
      const message = String(error instanceof Error ? error.message : error);
      if (gitUpdateStarted || !onUpdateApplication) {
        setActionError(message);
      } else {
        onUpdateApplication({
          app,
          configWarning: lang === "zh"
            ? `未读取到已登记部署配置，已从当前运行状态反推；提交前请重点核对 YAML。${message ? ` (${message})` : ""}`
            : `Could not load a registered deployment config, so the form was inferred from current runtime state. Review the YAML carefully before submitting.${message ? ` (${message})` : ""}`,
        });
      }
    } finally {
      setConfigBusy("");
      setUpdatingApp("");
    }
  };
  const openConfig = async (app: Application) => {
    const request = ++configRequest.current;
    setActionError("");
    setConfigBusy(app.stack);
    try {
      const config = await fetchDeploymentConfig({ token, name: app.stack });
      if (request !== configRequest.current) return;
      setDeploymentConfig(config);
      setDeploymentConfigFor(app.stack);
      setConfigTab(config.manifest ? "manifest" : "compose");
    } catch (error) {
      if (request !== configRequest.current) return;
      setActionError(String(error instanceof Error ? error.message : error));
    } finally {
      if (request === configRequest.current) setConfigBusy("");
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
    navigate(applicationPath(app.stack, "logs") + `?service=${encodeURIComponent(service.fullName)}`);
  };

  const openServiceLogs = (service: DashboardService, appServices: DashboardService[]) => {
    if (!service.fullName) {
      setActionError(lang === "zh" ? "该服务暂无可读取日志。" : "This service has no logs available.");
      return;
    }
    setActionError("");
    const stack = service.stack || appServices[0]?.stack || selectedStack;
    if (stack) navigate(applicationPath(stack, "logs") + `?service=${encodeURIComponent(service.fullName)}`);
  };

  const loadVersions = async (app: Application, message = "") => {
    const request = ++versionsRequest.current;
    setActionError("");
    setRollbackState({ app: app.stack, versions: [], loading: true, error: "", message, busyVersion: null });
    try {
      const result = await fetchServiceHistory({ token, name: app.stack });
      if (request !== versionsRequest.current) return;
      setRollbackState({
        app: app.stack,
        versions: result.versions || [],
        loading: false,
        error: "",
        message,
        busyVersion: null,
      });
    } catch (error) {
      if (request !== versionsRequest.current) return;
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
    navigate(applicationPath(app.stack, "versions"));
  };

  const rollbackToVersion = async (app: Application, version: number) => {
    const ok = await confirm({
      title: lang === "zh" ? `将 ${app.stack} 回滚到 v${version}？` : `Roll ${app.stack} back to v${version}?`,
      body: lang === "zh"
        ? <p>运行态会切换回 v{version} 的镜像并重建实例。这本身也是一次新部署，之后仍可回滚到当前版本。</p>
        : <p>The runtime switches back to the v{version} image and its allocation is recreated. This is itself a new deployment, so you can roll forward again afterwards.</p>,
      confirmLabel: lang === "zh" ? `回滚到 v${version}` : `Roll back to v${version}`,
    });
    if (!ok) return;
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
  const selectedConfigTabs: ConfigTab[] = [
    ...(selectedConfig?.manifest ? ["manifest" as const] : []),
    ...(selectedConfig?.composeContent ? ["compose" as const] : []),
  ];
  const selectedConfigContent = configTab === "compose" ? selectedConfig?.composeContent : selectedConfig?.manifest;
  const serviceCountLabel = (count: number) => lang === "zh" ? `${count} 个服务` : `${count} service${count === 1 ? "" : "s"}`;
  const replicaLabel = (running: number, desired: number) => lang === "zh" ? `${running}/${desired} 副本` : `${running}/${desired} replicas`;
  const logLabel = lang === "zh" ? "日志" : "Logs";
  const shellLabel = t(lang, "shell");
  const serviceIsRunning = (service: DashboardService) => {
    const status = serviceRuntimeStatus(service);
    return (service.running || 0) > 0 || ["running", "healthy"].includes(status);
  };
  useEffect(() => {
    setConfigBusy("");
    setConfigCopyNotice("");
    if (!selected) return;
    setActionError("");
    if (tab === "config") void openConfig(selected);
    if (tab === "versions") void loadVersions(selected);
    return () => { configRequest.current += 1; versionsRequest.current += 1; };
    // Route changes and manual refresh, not polling snapshots, trigger requests.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected?.stack, tab, token, detailRefresh]);
  const activeServices = route.service ? selected?.services.filter((service) => (service.fullName || service.name) === route.service) || [] : selected?.services || [];
  const detailPage = selected ? (
      <section className="application-detail-page application-workspace" aria-labelledby="application-detail-title">
        <nav className="breadcrumbs" aria-label={lang === "zh" ? "当前位置" : "Breadcrumb"}>
          <a href={toHref("/apps")} onClick={(event) => { if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return; event.preventDefault(); setSelected(null); }}>{lang === "zh" ? "应用" : "Applications"}</a>
          <span>/</span><span>{selected.stack}</span>{route.service ? <><span>/</span><span>{route.service}</span></> : null}
        </nav>
        <header className="application-detail-header">
          <div>
            <p className="eyebrow">{lang === "zh" ? "应用详情" : "Application"}</p>
            <h1 id="application-detail-title">{selected.stack}</h1>
            <span>{serviceCountLabel(selected.services.length)} · {replicaLabel(selected.running, selected.desired)}</span>
          </div>
          <div className="application-detail-actions">
            <button type="button" className="ghost danger" disabled={Boolean(actionBusy)} onClick={() => void restart(selected)}>{actionBusy === selected.stack ? t(lang, "restarting") : t(lang, "restart")}</button>
            <button type="button" className="primary" disabled={Boolean(configBusy || updatingApp)} onClick={() => void openUpdate(selected)}>
              {updatingApp === selected.stack ? (lang === "zh" ? "更新中..." : "Updating...") : configBusy === selected.stack ? t(lang, "loadingConfig") : t(lang, "updateApp")}
            </button>
          </div>
        </header>
        <nav className="workspace-tabs" aria-label={lang === "zh" ? "应用工作区" : "Application workspace"}>
          {APPLICATION_TABS.map((item) => <a key={item.id} href={toHref(applicationPath(selected.stack, item.id))} className={tab === item.id ? "active" : ""} aria-current={tab === item.id ? "page" : undefined} onClick={(event) => { if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return; event.preventDefault(); navigate(applicationPath(selected.stack, item.id)); }}>{lang === "zh" ? item.zh : item.en}</a>)}
        </nav>
        <div className="application-detail-body">
          {tab === "overview" ? <>
          <section className="application-detail-section application-runtime-summary">
            <h3>{lang === "zh" ? "运行状态" : "Runtime"}</h3>
            <ApplicationProperties items={[
              { label: t(lang, "status"), value: <StatePill value={selected.status} label={localizeState(lang, selected.status)} /> },
              { label: t(lang, "replicas"), value: `${selected.running}/${selected.desired}` },
              { label: t(lang, "region"), value: selected.regions.join(", ") || "-" },
              { label: t(lang, "nodes"), value: selected.nodes.join(", ") || "-" },
              { label: t(lang, "exposure"), value: selected.exposure },
            ]} />
          </section>
          <section className="application-detail-section">
            <h3>{t(lang, "accessAddress")}</h3>
            <div className="application-access-list">
              {applicationEndpoints(selected.services).length ? applicationEndpoints(selected.services).map((endpoint) => (
                endpoint.href
                  ? <a key={endpoint.address} href={endpoint.href} target="_blank" rel="noreferrer">{endpoint.address}</a>
                  : <span className="application-tcp-address" key={endpoint.address}><Badge value="TCP" /><CodeCell value={endpoint.address} /></span>
              )) : <p>{t(lang, "internalOnly")}</p>}
            </div>
          </section>
          </> : null}
          {tab === "versions" && selectedRollback ? (
            <section className="application-detail-section version-history-section">
              <div className="version-history-heading">
                <h3>{t(lang, "versions")}</h3>
                <button type="button" className="ghost" disabled={selectedRollback.loading || selectedRollback.busyVersion !== null} onClick={() => void loadVersions(selected)}>{selectedRollback.loading ? t(lang, "loadingHistory") : t(lang, "refresh")}</button>
              </div>
              {selectedRollback.message ? <div className="rollback-message">{selectedRollback.message}</div> : null}
              {selectedRollback.error ? <div className="alert alert-error"><span>{selectedRollback.error}</span></div> : null}
              {selectedRollback.loading ? (
                <p className="deployment-config-empty">{t(lang, "loadingHistory")}</p>
              ) : selectedRollback.versions.length ? (
                <div className="version-history-list">
                  {selectedRollback.versions.map((version, index) => {
                    const targetVersion = versionNumber(version.version);
                    const isCurrent = index === 0;
                    const isBusy = targetVersion !== null && selectedRollback.busyVersion === targetVersion;
                    return (
                      <ApplicationVersionEntry key={`${version.version ?? "unknown"}-${index}`}
                        version={`v${version.version ?? "-"}`} current={isCurrent}
                        image={version.image || "-"} imageLabel={t(lang, "image")}
                        submitted={versionSubmittedLabel(version.submitTime)} submittedLabel={t(lang, "submitted")}
                        stable={version.stable ? <Badge value={t(lang, "stableVersion")} /> : undefined}
                        action={<>
                          {isCurrent ? (
                            <Badge value={t(lang, "currentVersion")} />
                          ) : targetVersion === null ? (
                            <Badge value="-" />
                          ) : (
                            <button type="button" className="ghost" disabled={selectedRollback.busyVersion !== null} onClick={() => void rollbackToVersion(selected, targetVersion)}>
                              {isBusy ? t(lang, "rollingBack") : t(lang, "rollbackToVersion")}
                            </button>
                          )}
                        </>}
                      />
                    );
                  })}
                </div>
              ) : (
                <p className="deployment-config-empty">{t(lang, "noVersionHistory")}</p>
              )}
            </section>
          ) : null}
          {tab === "config" && !selectedConfig ? <div className="empty-inline">{configBusy ? t(lang, "loadingConfig") : t(lang, "noDeploymentConfig")}<button type="button" className="ghost" onClick={() => void openConfig(selected)}>{t(lang, "refresh")}</button></div> : null}
          {tab === "config" && selectedConfig ? (
            <section className="application-detail-section deployment-config-section">
              <div className="deployment-config-heading">
                <div>
                  <h3>{t(lang, "deploymentConfig")}</h3>
                  <span>{t(lang, "source")}: {selectedConfig.sourceName || "-"} · {t(lang, "lastUpdated")}: {formatTimestamp(selectedConfig.updatedAt)}</span>
                </div>
                <div className="deployment-config-tabs">
                  <button type="button" disabled={!selectedConfigContent} onClick={() => {
                    setConfigCopyNotice("");
                    void Promise.resolve().then(() => navigator.clipboard.writeText(selectedConfigContent || "")).then(() => setConfigCopyNotice(lang === "zh" ? "已复制完整配置" : "Full configuration copied")).catch(() => setConfigCopyNotice(lang === "zh" ? "复制失败，请在配置区域选择并复制" : "Copy failed; select and copy the configuration below"));
                  }}>{lang === "zh" ? "复制配置" : "Copy configuration"}</button>
                  {selectedConfigTabs.map((tab) => (
                    <button type="button" className={configTab === tab ? "active" : ""} key={tab} onClick={() => { setConfigTab(tab); setConfigCopyNotice(""); }}>
                      {tab === "compose" ? t(lang, "composeFile") : t(lang, "lumaManifest")}
                    </button>
                  ))}
                </div>
              </div>
              {configCopyNotice ? <p role="status">{configCopyNotice}</p> : null}
              {selectedConfigContent ? (
                <pre tabIndex={0} aria-label={t(lang, "deploymentConfig")} className="deployment-config-code"><code>{selectedConfigContent}</code></pre>
              ) : (
                <p className="deployment-config-empty">{t(lang, "noDeploymentConfig")}</p>
              )}
            </section>
          ) : null}
          {tab === "services" ? <section className="application-detail-section">
            <h3>{route.service || t(lang, "services")}</h3>
            {route.service && !activeServices.length ? <p>{lang === "zh" ? "服务不存在或已移除" : "Service not found or removed"}</p> : null}
            {!route.service ? <div className="table-wrap"><table className="data-table">
              <thead><tr><th>{t(lang, "services")}</th><th>{t(lang, "status")}</th><th>{t(lang, "replicas")}</th><th>{t(lang, "nodes")}</th><th>{t(lang, "actions")}</th></tr></thead>
              <tbody>{selected.services.map((service) => <tr key={service.fullName || service.name}>
                <td><a href={toHref(applicationPath(selected.stack, "services", service.fullName || service.name))} onClick={(event) => { if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return; event.preventDefault(); navigate(applicationPath(selected.stack, "services", service.fullName || service.name)); }}>{service.name}</a><small className="muted">{service.image}</small></td>
                <td><StatePill label={localizeState(lang, serviceRuntimeStatus(service))} value={serviceRuntimeStatus(service)} /></td>
                <td>{service.running ?? 0}/{service.desired ?? 0}</td>
                <td>{(service.nodes || []).join(", ") || service.node || "-"}</td>
                <td><div className="app-action-row"><button type="button" className="ghost" disabled={!service.fullName} onClick={() => openServiceLogs(service, selected.services)}><FileText size={15} />{logLabel}</button><button type="button" className="ghost" disabled={!service.fullName || !serviceIsRunning(service) || !onServiceTerminal} title={!serviceIsRunning(service) ? (lang === "zh" ? "服务未运行，无法进入容器" : "Service is not running") : shellLabel} onClick={() => onServiceTerminal?.(service, selected.stack)}><SquareTerminal size={15} />{shellLabel}</button></div></td>
              </tr>)}</tbody>
            </table></div> : null}
            <div className="application-service-grid">
              {(route.service ? activeServices : []).map((service) => (
                <article className="application-service-detail" key={service.fullName || service.name}>
                  <div className="application-service-title">
                    <a href={toHref(applicationPath(selected.stack, "services", service.fullName || service.name))} onClick={(event) => { if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return; event.preventDefault(); navigate(applicationPath(selected.stack, "services", service.fullName || service.name)); }}>{service.name}</a>
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
                      <button
                        type="button"
                        className="ghost service-log-button"
                        disabled={!service.fullName || !serviceIsRunning(service) || !onServiceTerminal}
                        title={
                          !service.fullName
                            ? (lang === "zh" ? "该服务还没有可进入的运行实例" : "This service has no runnable instance")
                            : !serviceIsRunning(service)
                              ? (lang === "zh" ? "服务未运行，无法进入容器" : "Service is not running")
                              : (lang === "zh" ? "进入该服务的容器终端" : "Open a shell in this service container")
                        }
                        onClick={() => onServiceTerminal?.(service, selected.stack)}
                      >
                        <SquareTerminal size={15} aria-hidden="true" />
                        {shellLabel}
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
          </section> : null}
          {tab === "overview" ? <section className="application-detail-section">
            <h3>{lang === "zh" ? "存储与诊断" : "Storage and diagnostics"}</h3>
            <div className="application-diagnostics-list">
              {selectedVolumes.length ? selectedVolumes.map((volume, index) => (
                <article className="application-volume-entry" key={`${volume.name}-${volume.storageClass}-${volume.node}-${index}`}>
                  <ApplicationProperties items={[
                    { label: lang === "zh" ? "卷 / 路径" : "Volume / path", value: <code className="application-full-value">{volume.name}</code> },
                    { label: lang === "zh" ? "类型" : "Type", value: volume.kind || "unmanaged" },
                    { label: lang === "zh" ? "存储类 / 节点" : "Storage class / node", value: volume.storageClass || volume.node || "-" },
                  ]} />
                </article>
              )) : <p>{lang === "zh" ? "未发现应用卷" : "No application volumes found"}</p>}
              {selectedDiagnostics.length ? selectedDiagnostics.map((item) => <p key={item}>{item}</p>) : <p>{lang === "zh" ? "暂无诊断告警" : "No diagnostic warnings"}</p>}
            </div>
          </section> : null}
          {tab === "logs" ? <ServiceLogsModal key={selected.stack} inline lang={lang} token={token} services={selected.services} initialServiceName={new URLSearchParams(search).get("service") || selected.services.find((service) => service.fullName)?.fullName || ""} onClose={() => navigate(applicationPath(selected.stack, "overview"))} /> : null}
          {tab === "metrics" ? <ObservabilityPanel key={selected.stack} lang={lang} token={token} services={selected.services} nodes={[]} /> : null}
        </div>
      </section>
  ) : null;
  const moreLabel = lang === "zh" ? "更多操作" : "More actions";

  const renderActions = (app: Application, compact: boolean) => {
    const hasLogs = Boolean(firstLogService(app));
    const menuOpen = openMenu === app.stack;
    return (
      <div
        className="app-action-row"
        ref={menuOpen ? menuRef : undefined}
        onClick={(event) => event.stopPropagation()}
        onKeyDown={(event) => event.stopPropagation()}
      >
        <button
          type="button"
          className="ghost app-log-action"
          disabled={!hasLogs}
          onClick={() => openApplicationLogs(app)}
        >
          <FileText size={15} aria-hidden="true" />
          {logLabel}
        </button>
        <button
          type="button"
          className="ghost"
          disabled={Boolean(configBusy || updatingApp)}
          onClick={() => void openUpdate(app)}
        >
          {updatingApp === app.stack ? <Loader2 size={15} aria-hidden="true" className="spin" /> : <Pencil size={15} aria-hidden="true" />}
          {updatingApp === app.stack ? (lang === "zh" ? "更新中..." : "Updating...") : configBusy === app.stack ? t(lang, "loadingConfig") : t(lang, "updateApp")}
        </button>
        {compact ? (
          <div className="app-action-menu">
            <button
              type="button"
              className="ghost app-action-menu-trigger"
              aria-label={moreLabel}
              aria-expanded={menuOpen}
              aria-haspopup="menu"
              onClick={() => setOpenMenu(menuOpen ? null : app.stack)}
            >
              <MoreHorizontal size={15} aria-hidden="true" />
            </button>
            {menuOpen ? (
              <div className="app-action-menu-panel" role="menu">
                <button type="button" role="menuitem" onClick={() => { setOpenMenu(null); openDetails(app); }}>
                  <Settings2 size={14} aria-hidden="true" />
                  {t(lang, "details")}
                </button>
                <button
                  type="button"
                  role="menuitem"
                  disabled={rollbackState?.app === app.stack && rollbackState.loading}
                  onClick={() => { setOpenMenu(null); void openVersions(app); }}
                >
                  <History size={14} aria-hidden="true" />
                  {rollbackState?.app === app.stack && rollbackState.loading ? t(lang, "loadingHistory") : t(lang, "versions")}
                </button>
                <button
                  type="button"
                  role="menuitem"
                  className="danger"
                  disabled={Boolean(actionBusy)}
                  onClick={() => { setOpenMenu(null); void restart(app); }}
                >
                  <RotateCw size={14} aria-hidden="true" />
                  {actionBusy === app.stack ? t(lang, "restarting") : t(lang, "restart")}
                </button>
              </div>
            ) : null}
          </div>
        ) : (
          <>
            <button type="button" className="ghost" onClick={() => openDetails(app)}>
              <Settings2 size={15} aria-hidden="true" />
              {t(lang, "details")}
            </button>
            <button type="button" className="ghost" disabled={rollbackState?.app === app.stack && rollbackState.loading} onClick={() => void openVersions(app)}>
              <History size={15} aria-hidden="true" />
              {rollbackState?.app === app.stack && rollbackState.loading ? t(lang, "loadingHistory") : t(lang, "versions")}
            </button>
            <button type="button" className="ghost danger" disabled={Boolean(actionBusy)} onClick={() => void restart(app)}>
              <RotateCw size={15} aria-hidden="true" />
              {actionBusy === app.stack ? t(lang, "restarting") : t(lang, "restart")}
            </button>
          </>
        )}
      </div>
    );
  };

  return (
    <article className={`panel app-management-panel${selected ? " has-application-workspace" : ""}`} id="section-1">
      {selectedStack && !selected ? <div className="alert alert-warning"><span>{lang === "zh" ? `未找到应用 ${selectedStack}，可能已删除或当前账号无法访问。` : `Application ${selectedStack} was not found. It may have been removed or is unavailable to this account.`}</span><button className="ghost" type="button" onClick={() => setSelected(null)}>{lang === "zh" ? "返回列表" : "Back to list"}</button></div> : null}
      {actionError ? <div className="alert alert-error"><span>{actionError}</span></div> : null}
      {actionNotice ? <div className="alert alert-success"><span>{actionNotice}</span></div> : null}
      {actionSteps.length || updatingApp ? <section className="application-detail-section" aria-live="polite"><h3>{lang === "zh" ? "应用更新进度" : "Application update progress"}</h3><StepLog steps={actionSteps} lang={lang} waitingLabel={updatingApp ? (lang === "zh" ? "正在开始更新…" : "Starting update…") : undefined} /><button type="button" className="ghost" onClick={onNavigateToDeployments}>{lang === "zh" ? "查看交付记录" : "View delivery history"}</button></section> : null}
      {!selectedStack ? <>
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
          <SelectControl
            value={filters.status}
            onChange={(value) => setFilters((current) => ({ ...current, status: value }))}
            options={[
              { value: "all", label: lang === "zh" ? "全部状态" : "All statuses" },
              ...statusOptions.map((status) => ({ value: status, label: localizeState(lang, status) })),
            ]}
          />
        </label>
        <label>
          <span>{t(lang, "region")}</span>
          <SelectControl
            value={filters.region}
            onChange={(value) => setFilters((current) => ({ ...current, region: value }))}
            options={[
              { value: "all", label: lang === "zh" ? "全部区域" : "All regions" },
              ...regionOptions.map((region) => ({ value: region, label: region })),
            ]}
          />
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
                <td>{renderActions(app, true)}</td>
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
              {renderActions(app, false)}
            </div>
          </article>
        )) : <div className="empty-inline">{t(lang, "noApplications")}</div>}
      </div>

      </> : null}
      {detailPage}
      {confirmDialog}
    </article>
  );
}
