"use client";

import {
  Activity,
  ArrowLeft,
  ArrowUpRight,
  Boxes,
  CheckCircle2,
  ChevronRight,
  CircleUserRound,
  CloudUpload,
  Command,
  ExternalLink,
  FileClock,
  Gauge,
  GitCompareArrows,
  Globe2,
  KeyRound,
  Layers3,
  ListRestart,
  LoaderCircle,
  LockKeyhole,
  Moon,
  Pause,
  Play,
  RefreshCw,
  RotateCcw,
  Server,
  Settings2,
  ShieldCheck,
  SquareTerminal,
  Sun,
  Trash2,
  Undo2,
  X,
  XCircle,
} from "lucide-react";
import Link from "next/link";
import {
  FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";

import {
  cancelOperation,
  createDeployment,
  getApplication,
  getApplicationLogs,
  getApplicationMetrics,
  getOperation,
  getOperationEvents,
  getPrincipal,
  LaeApiError,
  listApplicationDeployments,
  newIdempotencyKey,
  patchApplicationEnvironment,
  requestApplicationAction,
  type ApplicationAction,
  type ApplicationDeployment,
  type ApplicationLogTail,
  type ApplicationMetricHistory,
  type ApplicationRecord,
  type LaePrincipal,
  type Operation,
  type OperationEvent,
  type UpdateConfirmationCode,
} from "../lib/lae-api";
import styles from "./application-detail-console.module.css";

type ViewState = "loading" | "ready" | "guest" | "not-found" | "error";
type Theme = "light" | "dark";
type Confirmation = {
  action: "suspend" | "rollback" | "deploy-update";
  deploymentId?: string;
  analysisId?: string;
  confirmedChanges?: UpdateConfirmationCode[];
  title: string;
  note: string;
};

const ENVIRONMENT_NAME = /^[A-Z_][A-Z0-9_]{0,127}$/;

export function ApplicationDetailConsole({
  applicationId,
}: {
  applicationId: string;
}) {
  const [theme, setTheme] = useState<Theme>("dark");
  const [viewState, setViewState] = useState<ViewState>("loading");
  const [reloadVersion, setReloadVersion] = useState(0);
  const [principal, setPrincipal] = useState<LaePrincipal | null>(null);
  const [record, setRecord] = useState<ApplicationRecord | null>(null);
  const [deployments, setDeployments] = useState<ApplicationDeployment[]>([]);
  const [pageError, setPageError] = useState<string | null>(null);

  const [selectedService, setSelectedService] = useState("");
  const [logs, setLogs] = useState<ApplicationLogTail | null>(null);
  const [metrics, setMetrics] = useState<ApplicationMetricHistory | null>(null);
  const [observabilityLoading, setObservabilityLoading] = useState(false);
  const [observabilityNotice, setObservabilityNotice] = useState<string | null>(null);
  const [observabilityRefresh, setObservabilityRefresh] = useState(0);

  const [activeOperationId, setActiveOperationId] = useState<string | null>(null);
  const [operation, setOperation] = useState<Operation | null>(null);
  const [operationEvents, setOperationEvents] = useState<OperationEvent[]>([]);
  const [operationError, setOperationError] = useState<string | null>(null);
  const [actionBusy, setActionBusy] = useState(false);
  const [actionNotice, setActionNotice] = useState<string | null>(null);
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);

  const [environmentOpen, setEnvironmentOpen] = useState(false);
  const [environmentScope, setEnvironmentScope] = useState("");
  const [environmentName, setEnvironmentName] = useState("");
  const [environmentValue, setEnvironmentValue] = useState("");
  const [environmentSensitive, setEnvironmentSensitive] = useState(true);
  const [environmentRequired, setEnvironmentRequired] = useState(false);
  const [editingEnvironmentRef, setEditingEnvironmentRef] = useState<string | null>(null);
  const [pendingUnsetRef, setPendingUnsetRef] = useState<string | null>(null);
  const [environmentSaving, setEnvironmentSaving] = useState(false);
  const [environmentError, setEnvironmentError] = useState<string | null>(null);

  const refreshSnapshot = useCallback(
    async (signal?: AbortSignal) => {
      const [nextRecord, deploymentPage] = await Promise.all([
        getApplication(applicationId, signal),
        listApplicationDeployments(applicationId, 30, signal),
      ]);
      setRecord(nextRecord);
      setDeployments(deploymentPage.deployments);
      const serviceKeys = nextRecord.services.map((service) => service.key);
      setSelectedService((current) =>
        serviceKeys.includes(current)
          ? current
          : nextRecord.services.find((service) => service.role === "http")?.key ||
            serviceKeys[0] ||
            "",
      );
      return { record: nextRecord, deployments: deploymentPage.deployments };
    },
    [applicationId],
  );

  useEffect(() => {
    const stored = window.localStorage.getItem("luma.dashboard.theme");
    const initial: Theme = stored === "light" ? "light" : "dark";
    setTheme(initial);
    document.documentElement.dataset.theme = initial;
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setViewState("loading");
    setPageError(null);

    void Promise.all([
      getPrincipal(controller.signal),
      refreshSnapshot(controller.signal),
    ])
      .then(([nextPrincipal, snapshot]) => {
        if (controller.signal.aborted) return;
        setPrincipal(nextPrincipal);
        setViewState("ready");
        const currentDeployment = snapshot.deployments.find(
          (item) => item.id === snapshot.record.application.currentDeploymentId,
        );
        const latestDeployment = currentDeployment || snapshot.deployments[0];
        if (latestDeployment) setActiveOperationId(latestDeployment.operationId);
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if (error instanceof LaeApiError && error.status === 401) {
          setViewState("guest");
          return;
        }
        if (error instanceof LaeApiError && error.status === 404) {
          setViewState("not-found");
          return;
        }
        setViewState("error");
        setPageError(apiErrorMessage(error, "应用详情暂时无法读取。"));
      });

    return () => controller.abort();
  }, [refreshSnapshot, reloadVersion]);

  useEffect(() => {
    if (!activeOperationId) {
      setOperation(null);
      setOperationEvents([]);
      return;
    }

    const controller = new AbortController();
    let cursor = 0;
    let timer: number | undefined;
    setOperation(null);
    setOperationEvents([]);
    setOperationError(null);

    const poll = async () => {
      try {
        const [nextOperation, eventPage] = await Promise.all([
          getOperation(activeOperationId, controller.signal),
          getOperationEvents(activeOperationId, cursor, controller.signal),
        ]);
        if (controller.signal.aborted) return;
        setOperation(nextOperation);
        cursor = eventPage.cursor;
        setOperationEvents((current) => {
          const seen = new Set(current.map((event) => event.eventId));
          return [...current, ...eventPage.events.filter((event) => !seen.has(event.eventId))]
            .sort((left, right) => left.cursor - right.cursor);
        });

        if (nextOperation.terminal && !eventPage.hasMore) {
          void refreshSnapshot(controller.signal).catch(() => undefined);
          return;
        }
        timer = window.setTimeout(poll, eventPage.hasMore ? 300 : 1_500);
      } catch (error) {
        if (controller.signal.aborted) return;
        setOperationError(apiErrorMessage(error, "操作进度暂时无法读取。"));
        timer = window.setTimeout(poll, 3_000);
      }
    };

    void poll();
    return () => {
      controller.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [activeOperationId, refreshSnapshot]);

  useEffect(() => {
    const deploymentId = record?.application.currentDeploymentId;
    if (!record || !deploymentId || !selectedService) {
      setLogs(null);
      setMetrics(null);
      setObservabilityNotice(null);
      return;
    }

    const controller = new AbortController();
    let loading = false;
    const load = async () => {
      if (loading) return;
      loading = true;
      setObservabilityLoading(true);
      const [logResult, metricResult] = await Promise.allSettled([
        getApplicationLogs(
          applicationId,
          { service: selectedService, tail: 180 },
          controller.signal,
        ),
        getApplicationMetrics(
          applicationId,
          { service: selectedService, window: 3_600 },
          controller.signal,
        ),
      ]);
      loading = false;
      if (controller.signal.aborted) return;

      const notices: string[] = [];
      if (logResult.status === "fulfilled") setLogs(logResult.value);
      else {
        setLogs(null);
        notices.push(apiErrorMessage(logResult.reason, "日志暂不可用。"));
      }
      if (metricResult.status === "fulfilled") setMetrics(metricResult.value);
      else {
        setMetrics(null);
        notices.push(apiErrorMessage(metricResult.reason, "指标暂不可用。"));
      }
      setObservabilityNotice(notices.length ? notices.join(" ") : null);
      setObservabilityLoading(false);
    };

    void load();
    const interval = window.setInterval(load, 15_000);
    return () => {
      controller.abort();
      window.clearInterval(interval);
    };
  }, [applicationId, observabilityRefresh, record, selectedService]);

  const sortedDeployments = useMemo(
    () =>
      [...deployments].sort(
        (left, right) => Date.parse(right.createdAt) - Date.parse(left.createdAt),
      ),
    [deployments],
  );
  const rollbackTarget = useMemo(
    () =>
      sortedDeployments.find(
        (item) =>
          item.status === "succeeded" &&
          item.id !== record?.application.currentDeploymentId,
      ) || null,
    [record?.application.currentDeploymentId, sortedDeployments],
  );
  const environmentScopes = useMemo(
    () => (record?.services.length ? record.services.map((service) => service.key) : ["*"]),
    [record?.services],
  );
  const operationRunning = Boolean(
    activeOperationId && (!operation || !operation.terminal),
  );
  const interactionLocked = actionBusy || operationRunning;
  const application = record?.application;
  const primaryRoute = record?.routes.find((route) => route.primary) || record?.routes[0];
  const suspended = application?.desiredState === "suspended" || application?.observedState === "suspended";

  useEffect(() => {
    if (!environmentScope && environmentScopes.length) {
      setEnvironmentScope(environmentScopes[0]);
    }
  }, [environmentScope, environmentScopes]);

  const toggleTheme = () => {
    const next: Theme = theme === "dark" ? "light" : "dark";
    setTheme(next);
    document.documentElement.dataset.theme = next;
    window.localStorage.setItem("luma.dashboard.theme", next);
  };

  const runAction = async (
    action: ApplicationAction,
    input: { deploymentId?: string } = {},
  ) => {
    setActionBusy(true);
    setActionNotice(null);
    setOperationError(null);
    try {
      const result = await requestApplicationAction(
        applicationId,
        action,
        input,
        newIdempotencyKey(`application-${action}`),
      );
      setActiveOperationId(result.operation.id);
      setActionNotice(actionStartMessage(action));
      setConfirmation(null);
    } catch (error) {
      setActionNotice(apiErrorMessage(error, "操作请求未能提交。"));
    } finally {
      setActionBusy(false);
    }
  };

  const deployUpdate = async (
    analysisId: string,
    confirmedChanges: Confirmation["confirmedChanges"] = [],
  ) => {
    if (!record) return;
    setActionBusy(true);
    setActionNotice(null);
    setOperationError(null);
    try {
      const result = await createDeployment(
        {
          applicationId,
          analysisId,
          environmentVersion: record.environment.version,
          confirmedChanges,
        },
        newIdempotencyKey("application-update-deploy"),
      );
      setActiveOperationId(result.operation.id);
      setActionNotice("更新部署已启动；当前健康版本会保留到新版本验证成功。");
      setConfirmation(null);
    } catch (error) {
      setActionNotice(apiErrorMessage(error, "更新部署未能提交。"));
    } finally {
      setActionBusy(false);
    }
  };

  const cancelActiveOperation = async () => {
    if (!activeOperationId || !operationRunning) return;
    setActionBusy(true);
    setActionNotice(null);
    try {
      const next = await cancelOperation(activeOperationId);
      setOperation(next);
      setActionNotice("取消请求已提交；正在等待当前阶段到达安全中断点。");
    } catch (error) {
      setActionNotice(apiErrorMessage(error, "当前操作无法取消。"));
    } finally {
      setActionBusy(false);
    }
  };

  const resetEnvironmentForm = () => {
    setEditingEnvironmentRef(null);
    setEnvironmentName("");
    setEnvironmentValue("");
    setEnvironmentSensitive(true);
    setEnvironmentRequired(false);
    setEnvironmentError(null);
    setEnvironmentScope(environmentScopes[0] || "*");
  };

  const editEnvironment = (
    item: ApplicationRecord["environment"]["variables"][number],
  ) => {
    setEnvironmentOpen(true);
    setEditingEnvironmentRef(`${item.serviceScope}:${item.name}`);
    setEnvironmentScope(item.serviceScope);
    setEnvironmentName(item.name);
    setEnvironmentValue("");
    setEnvironmentSensitive(item.sensitive);
    setEnvironmentRequired(item.required);
    setEnvironmentError(null);
  };

  const saveEnvironment = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!record) return;
    const name = environmentName.trim().toUpperCase();
    if (!environmentScopes.includes(environmentScope)) {
      setEnvironmentError("请选择一个有效的服务作用域。");
      return;
    }
    if (!ENVIRONMENT_NAME.test(name)) {
      setEnvironmentError("变量名必须以大写字母或下划线开头，且只包含大写字母、数字和下划线。");
      return;
    }
    if (!environmentValue.length) {
      setEnvironmentError("请输入新值。现有值不会从服务端回传。 ");
      return;
    }

    setEnvironmentSaving(true);
    setEnvironmentError(null);
    try {
      await patchApplicationEnvironment(
        applicationId,
        {
          expectedVersion: record.environment.version,
          set: {
            [`${environmentScope}:${name}`]: {
              value: environmentValue,
              sensitive: environmentSensitive,
              required: environmentRequired,
            },
          },
        },
        newIdempotencyKey("application-environment"),
      );
      await refreshSnapshot();
      resetEnvironmentForm();
      setActionNotice(editingEnvironmentRef ? "环境变量已更新。" : "环境变量已添加。");
    } catch (error) {
      if (error instanceof LaeApiError && error.status === 409) {
        void refreshSnapshot().catch(() => undefined);
        setEnvironmentError("环境配置已被其他操作更新，页面已刷新，请重新提交。");
      } else {
        setEnvironmentError(apiErrorMessage(error, "环境变量未能保存。"));
      }
    } finally {
      setEnvironmentSaving(false);
    }
  };

  const unsetEnvironment = async (reference: string) => {
    if (!record) return;
    setEnvironmentSaving(true);
    setEnvironmentError(null);
    try {
      await patchApplicationEnvironment(
        applicationId,
        {
          expectedVersion: record.environment.version,
          set: {},
          unset: [reference],
        },
        newIdempotencyKey("application-environment-unset"),
      );
      await refreshSnapshot();
      setPendingUnsetRef(null);
      setActionNotice("环境变量已移除；需要重启或重新部署时，操作会在真实任务中体现。");
    } catch (error) {
      setEnvironmentError(apiErrorMessage(error, "环境变量未能移除。"));
    } finally {
      setEnvironmentSaving(false);
    }
  };

  if (viewState !== "ready" || !record || !application) {
    return (
      <main className={`console-shell ${styles.shell}`}>
        <DetailRail />
        <div className="console-main">
          <SimpleTopbar theme={theme} onToggleTheme={toggleTheme} />
          <section className={`workspace ${styles.gateWorkspace}`}>
            <StateGate
              state={viewState}
              error={pageError}
              onRetry={() => setReloadVersion((value) => value + 1)}
            />
          </section>
        </div>
      </main>
    );
  }

  return (
    <main className={`console-shell ${styles.shell}`}>
      <DetailRail />
      <div className="console-main">
        <header className="topbar">
          <div className={`topbar-breadcrumbs ${styles.breadcrumbs}`}>
            <Link href="/">LAE</Link>
            <ChevronRight size={12} />
            <Link href="/#applications">应用</Link>
            <ChevronRight size={12} />
            <strong>{application.name}</strong>
          </div>
          <div className="runtime-status">
            <span
              className={`runtime-pulse${application.observedState === "running" ? "" : " is-muted"}`}
            />
            {stateLabel(application.observedState)}
          </div>
          <div className="account">
            <button
              className="theme-toggle"
              type="button"
              onClick={toggleTheme}
              aria-label={theme === "dark" ? "切换到浅色主题" : "切换到深色主题"}
            >
              {theme === "dark" ? <Sun size={15} /> : <Moon size={15} />}
            </button>
            <span className="plan-badge">{principal?.entitlement.plan.toUpperCase()}</span>
            <Link className="account-button" href="/account">
              <CircleUserRound size={18} strokeWidth={1.5} />
              <span>{principal?.user.email.split("@", 1)[0]}</span>
              <ChevronRight size={14} />
            </Link>
          </div>
        </header>

        <section className={`workspace ${styles.workspace}`}>
          <div className={styles.hero}>
            <div>
              <Link className={styles.backLink} href="/#applications">
                <ArrowLeft size={13} /> 返回应用列表
              </Link>
              <p className="section-kicker">APPLICATION CONTROL</p>
              <div className={styles.titleLine}>
                <h1>{application.name}</h1>
                <StatusPill value={application.observedState} />
              </div>
              <span>
                {application.kind.toUpperCase()} · {application.slug} · {shortId(application.id)}
              </span>
            </div>
            {primaryRoute ? (
              <a
                className={styles.openApplication}
                href={`https://${primaryRoute.hostname}`}
                target="_blank"
                rel="noreferrer"
              >
                打开应用 <ArrowUpRight size={14} />
              </a>
            ) : null}
          </div>

          <div className={styles.overview} aria-label="应用概览">
            <OverviewCell
              label="期望状态"
              value={stateLabel(application.desiredState)}
              note={`实际：${stateLabel(application.observedState)}`}
            />
            <OverviewCell
              label="服务"
              value={String(record.services.length)}
              note={`${record.services.filter((service) => service.role === "http").length} HTTP`}
            />
            <OverviewCell
              label="公网路由"
              value={String(record.routes.length)}
              note="HTTP / HTTPS"
            />
            <OverviewCell
              label="当前修订"
              value={shortId(application.currentRevisionId)}
              note={formatTimestamp(application.updatedAt)}
            />
          </div>

          <section className={`${styles.panel} ${styles.controlPanel}`} aria-labelledby="controls-title">
            <PanelHeading
              eyebrow="RUNTIME CONTROL"
              title="运行控制"
              note="所有动作都提交到 LAE API，并以 operation 事件确认最终结果。"
              icon={ListRestart}
              id="controls-title"
            />
            <div className={styles.actionBar}>
              {suspended ? (
                <button
                  className={styles.primaryButton}
                  type="button"
                  disabled={interactionLocked}
                  onClick={() => void runAction("resume")}
                >
                  <Play size={14} /> 恢复
                </button>
              ) : (
                <button
                  type="button"
                  disabled={interactionLocked}
                  onClick={() =>
                    setConfirmation({
                      action: "suspend",
                      title: "停止这个应用？",
                      note: "LAE 会将期望状态设为 suspended，并等待运行实例安全停止。公网路由信息会保留。",
                    })
                  }
                >
                  <Pause size={14} /> 停止
                </button>
              )}
              <button
                type="button"
                disabled={interactionLocked || suspended}
                onClick={() => void runAction("restart")}
              >
                <RefreshCw size={14} /> 重启
              </button>
              <button
                type="button"
                disabled={interactionLocked}
                onClick={() => void runAction("check-update")}
              >
                <GitCompareArrows size={14} /> 检查更新
              </button>
              <button
                type="button"
                disabled={interactionLocked || !rollbackTarget}
                title={rollbackTarget ? `回滚到 ${shortId(rollbackTarget.id)}` : "没有可用的成功部署"}
                onClick={() =>
                  rollbackTarget &&
                  setConfirmation({
                    action: "rollback",
                    deploymentId: rollbackTarget.id,
                    title: "回滚到上一个成功部署？",
                    note: `目标部署 ${shortId(rollbackTarget.id)}，创建于 ${formatTimestamp(rollbackTarget.createdAt)}。LAE 会保留当前部署记录。`,
                  })
                }
              >
                <Undo2 size={14} /> 回滚
              </button>
              {operationRunning ? (
                <button
                  className={styles.dangerButton}
                  type="button"
                  disabled={actionBusy}
                  onClick={() => void cancelActiveOperation()}
                >
                  <X size={14} /> 取消操作
                </button>
              ) : null}
            </div>
            {actionNotice ? <p className={styles.inlineNotice}>{actionNotice}</p> : null}
          </section>

          <div className={styles.twoColumn}>
            <section className={styles.panel} aria-labelledby="services-title">
              <PanelHeading
                eyebrow="SERVICE TOPOLOGY"
                title="服务状态"
                note={`${record.services.length} 个服务由当前部署计划管理。`}
                icon={Layers3}
                id="services-title"
              />
              {record.services.length ? (
                <div className={styles.serviceList}>
                  {record.services.map((service) => (
                    <article key={service.key}>
                      <div className={styles.serviceIdentity}>
                        <Server size={14} />
                        <div>
                          <strong>{service.key}</strong>
                          <span>
                            {service.role} · desired {stateLabel(service.desiredState)} · {service.required ? "required" : "optional"}
                          </span>
                        </div>
                      </div>
                      <div className={styles.serviceState}>
                        <StatusPill value={service.observedState} />
                        <small>{shortDigest(service.currentImageDigest)}</small>
                      </div>
                    </article>
                  ))}
                </div>
              ) : (
                <EmptyState icon={Server} text="尚未生成可运行的服务拓扑。" />
              )}
            </section>

            <section className={styles.panel} aria-labelledby="routes-title">
              <PanelHeading
                eyebrow="PUBLIC HTTP"
                title="全部路由"
                note="Compose 应用的每个公网 HTTP 服务都会保留独立路由。"
                icon={Globe2}
                id="routes-title"
              />
              {record.routes.length ? (
                <div className={styles.routeList}>
                  {record.routes.map((route) => (
                    <article key={`${route.serviceKey}:${route.hostname}`}>
                      <div>
                        <span className={styles.routeIcon}><Globe2 size={13} /></span>
                        <div>
                          <a href={`https://${route.hostname}`} target="_blank" rel="noreferrer">
                            {route.hostname} <ExternalLink size={11} />
                          </a>
                          <span>{route.serviceKey}:{route.containerPort}</span>
                        </div>
                      </div>
                      <div className={styles.routeMeta}>
                        {route.primary ? <small>PRIMARY</small> : null}
                        <StatusPill value={route.status} />
                      </div>
                    </article>
                  ))}
                </div>
              ) : (
                <EmptyState icon={Globe2} text="当前部署没有公开 HTTP 路由。" />
              )}
            </section>
          </div>

          <section className={styles.panel} aria-labelledby="deployments-title">
            <PanelHeading
              eyebrow="DELIVERY HISTORY"
              title="部署与操作"
              note="部署记录不可变；选中任意记录可查看对应 operation 的真实事件。"
              icon={FileClock}
              id="deployments-title"
            />
            <div className={styles.deliveryGrid}>
              <div className={styles.deploymentTableWrap}>
                {sortedDeployments.length ? (
                  <table className={styles.deploymentTable}>
                    <thead>
                      <tr>
                        <th>部署</th>
                        <th>修订</th>
                        <th>状态</th>
                        <th>时间</th>
                        <th><span className={styles.srOnly}>查看操作</span></th>
                      </tr>
                    </thead>
                    <tbody>
                      {sortedDeployments.map((deployment) => (
                        <tr
                          key={deployment.id}
                          className={activeOperationId === deployment.operationId ? styles.activeRow : undefined}
                        >
                          <td>
                            <strong>{shortId(deployment.id)}</strong>
                            {deployment.id === application.currentDeploymentId ? <small>CURRENT</small> : null}
                          </td>
                          <td>{shortId(deployment.revisionId)}</td>
                          <td>
                            <StatusPill value={deployment.status} />
                            {deployment.error ? (
                              <small className={styles.deploymentError} title={deployment.error.message}>
                                {deployment.error.code}
                              </small>
                            ) : null}
                          </td>
                          <td>{formatTimestamp(deployment.finishedAt || deployment.createdAt)}</td>
                          <td>
                            <button
                              type="button"
                              onClick={() => setActiveOperationId(deployment.operationId)}
                              aria-label={`查看部署 ${shortId(deployment.id)} 的操作`}
                            >
                              <ChevronRight size={13} />
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : (
                  <EmptyState icon={FileClock} text="这个应用还没有部署记录。" />
                )}
              </div>
              <OperationInspector
                operationId={activeOperationId}
                operation={operation}
                events={operationEvents}
                error={operationError}
                interactionLocked={interactionLocked}
                onDeployUpdate={(analysisId, confirmations, note) => {
                  const destructive = confirmations.length > 0;
                  if (destructive) {
                    setConfirmation({
                      action: "deploy-update",
                      analysisId,
                      confirmedChanges: confirmations,
                      title: "确认部署破坏性更新？",
                      note,
                    });
                    return;
                  }
                  void deployUpdate(analysisId, []);
                }}
              />
            </div>
          </section>

          <section className={styles.panel} id="environment" aria-labelledby="environment-title">
            <div className={styles.panelHeaderWithAction}>
              <PanelHeading
                eyebrow={`CONFIGURATION · VERSION ${record.environment.version}`}
                title="环境变量"
                note="值只写入密钥存储，API 仅返回配置元数据；更新时需要输入完整新值。"
                icon={KeyRound}
                id="environment-title"
              />
              <button
                type="button"
                onClick={() => {
                  setEnvironmentOpen((value) => !value);
                  resetEnvironmentForm();
                }}
              >
                <Settings2 size={13} /> {environmentOpen ? "收起管理" : "管理变量"}
              </button>
            </div>

            {environmentError ? <p className={styles.errorNotice}>{environmentError}</p> : null}
            <div className={styles.environmentLayout}>
              <div className={styles.environmentList}>
                {record.environment.variables.length ? (
                  record.environment.variables.map((item) => {
                    const reference = `${item.serviceScope}:${item.name}`;
                    return (
                      <article key={reference}>
                        <div className={styles.environmentIdentity}>
                          {item.sensitive ? <LockKeyhole size={13} /> : <SquareTerminal size={13} />}
                          <div>
                            <strong>{item.name}</strong>
                            <span>{item.serviceScope} · {item.source}</span>
                          </div>
                        </div>
                        <div className={styles.environmentMeta}>
                          <span>{item.configured ? "configured" : "missing"}</span>
                          {item.required ? <small>REQUIRED</small> : null}
                          <button type="button" onClick={() => editEnvironment(item)}>更新</button>
                          {item.configured ? (
                            pendingUnsetRef === reference ? (
                              <span className={styles.inlineConfirm}>
                                <button
                                  type="button"
                                  disabled={environmentSaving}
                                  onClick={() => void unsetEnvironment(reference)}
                                >确认</button>
                                <button type="button" onClick={() => setPendingUnsetRef(null)}>取消</button>
                              </span>
                            ) : (
                              <button
                                className={styles.iconDanger}
                                type="button"
                                aria-label={`移除 ${reference}`}
                                onClick={() => setPendingUnsetRef(reference)}
                              >
                                <Trash2 size={12} />
                              </button>
                            )
                          ) : null}
                        </div>
                      </article>
                    );
                  })
                ) : (
                  <EmptyState icon={KeyRound} text="当前部署计划没有声明环境变量；仍可按服务追加。" />
                )}
              </div>

              {environmentOpen ? (
                <form className={styles.environmentForm} onSubmit={saveEnvironment}>
                  <div>
                    <strong>{editingEnvironmentRef ? "替换变量值" : "追加环境变量"}</strong>
                    <button type="button" onClick={() => setEnvironmentOpen(false)} aria-label="关闭环境变量表单">
                      <X size={13} />
                    </button>
                  </div>
                  <label>
                    <span>目标服务</span>
                    <select
                      value={environmentScope}
                      disabled={Boolean(editingEnvironmentRef)}
                      onChange={(event) => setEnvironmentScope(event.target.value)}
                    >
                      {environmentScopes.map((scope) => <option key={scope} value={scope}>{scope}</option>)}
                    </select>
                  </label>
                  <label>
                    <span>变量名</span>
                    <input
                      value={environmentName}
                      disabled={Boolean(editingEnvironmentRef)}
                      onChange={(event) => setEnvironmentName(event.target.value.toUpperCase())}
                      placeholder="DATABASE_URL"
                      autoComplete="off"
                      spellCheck={false}
                    />
                  </label>
                  <label>
                    <span>新值</span>
                    <input
                      type={environmentSensitive ? "password" : "text"}
                      value={environmentValue}
                      onChange={(event) => setEnvironmentValue(event.target.value)}
                      autoComplete="new-password"
                      spellCheck={false}
                      placeholder={editingEnvironmentRef ? "输入完整替换值" : "输入变量值"}
                    />
                  </label>
                  <div className={styles.environmentChecks}>
                    <label>
                      <input
                        type="checkbox"
                        checked={environmentSensitive}
                        onChange={(event) => setEnvironmentSensitive(event.target.checked)}
                      />
                      敏感值
                    </label>
                    <label>
                      <input
                        type="checkbox"
                        checked={environmentRequired}
                        onChange={(event) => setEnvironmentRequired(event.target.checked)}
                      />
                      必填
                    </label>
                  </div>
                  <p><ShieldCheck size={12} /> 保存后无法从控制台读取明文。</p>
                  <button className={styles.primaryButton} type="submit" disabled={environmentSaving}>
                    {environmentSaving ? <LoaderCircle className={styles.spin} size={13} /> : <CheckCircle2 size={13} />}
                    {environmentSaving ? "正在保存" : editingEnvironmentRef ? "替换值" : "添加变量"}
                  </button>
                </form>
              ) : null}
            </div>
          </section>

          <section className={styles.panel} aria-labelledby="observability-title">
            <div className={styles.panelHeaderWithAction}>
              <PanelHeading
                eyebrow="LIVE OBSERVABILITY"
                title="日志与指标"
                note="读取当前部署的真实服务遥测；页面每 15 秒自动刷新。"
                icon={Activity}
                id="observability-title"
              />
              <div className={styles.observabilityControls}>
                <label>
                  <span className={styles.srOnly}>选择服务</span>
                  <select value={selectedService} onChange={(event) => setSelectedService(event.target.value)}>
                    {record.services.map((service) => (
                      <option key={service.key} value={service.key}>{service.key} · {service.role}</option>
                    ))}
                  </select>
                </label>
                <button type="button" onClick={() => setObservabilityRefresh((value) => value + 1)}>
                  <RefreshCw className={observabilityLoading ? styles.spin : undefined} size={13} /> 刷新
                </button>
              </div>
            </div>
            {observabilityNotice ? <p className={styles.errorNotice}>{observabilityNotice}</p> : null}
            {!application.currentDeploymentId ? (
              <EmptyState icon={Gauge} text="完成首次部署后才会产生运行日志与指标。" />
            ) : (
              <div className={styles.observabilityGrid}>
                <MetricBoard metrics={metrics} loading={observabilityLoading} />
                <LogTail logs={logs} loading={observabilityLoading} />
              </div>
            )}
          </section>

          {confirmation ? (
            <ConfirmationDialog
              value={confirmation}
              busy={actionBusy}
              onClose={() => setConfirmation(null)}
              onConfirm={() => {
                if (confirmation.action === "deploy-update") {
                  if (confirmation.analysisId) {
                    void deployUpdate(
                      confirmation.analysisId,
                      confirmation.confirmedChanges,
                    );
                  }
                  return;
                }
                void runAction(
                  confirmation.action,
                  confirmation.deploymentId ? { deploymentId: confirmation.deploymentId } : {},
                );
              }}
            />
          ) : null}
        </section>
      </div>
    </main>
  );
}

function DetailRail() {
  return (
    <aside className="rail" aria-label="主导航">
      <Link className="rail-brand" href="/" aria-label="Luma Application Engine">
        <div className="brand-mark"><span /><span /><span /></div>
        <div><strong>LAE</strong><small>Luma Application Engine</small></div>
      </Link>
      <p className="rail-label">APPLICATION ENGINE</p>
      <Link className="rail-button" href="/#deployment">
        <CloudUpload size={18} strokeWidth={1.7} /><span>部署</span>
      </Link>
      <Link className="rail-button" href="/#applications">
        <Boxes size={18} strokeWidth={1.7} /><span>应用</span>
      </Link>
      <span className="rail-button is-active" aria-current="page">
        <Activity size={18} strokeWidth={1.7} /><span>当前应用</span>
      </span>
      <div className="rail-spacer" />
      <Link className="rail-button" href="/#cli">
        <Command size={18} strokeWidth={1.7} /><span>CLI</span>
      </Link>
    </aside>
  );
}

function SimpleTopbar({ theme, onToggleTheme }: { theme: Theme; onToggleTheme: () => void }) {
  return (
    <header className="topbar">
      <div className="topbar-breadcrumbs">
        <span>LAE</span><ChevronRight size={12} /><strong>应用详情</strong>
      </div>
      <div className="runtime-status"><span className="runtime-pulse is-muted" />Waiting for API</div>
      <div className="account">
        <button
          className="theme-toggle"
          type="button"
          onClick={onToggleTheme}
          aria-label={theme === "dark" ? "切换到浅色主题" : "切换到深色主题"}
        >
          {theme === "dark" ? <Sun size={15} /> : <Moon size={15} />}
        </button>
        <Link className="account-button" href="/login"><CircleUserRound size={18} /><span>登录</span></Link>
      </div>
    </header>
  );
}

function StateGate({
  state,
  error,
  onRetry,
}: {
  state: ViewState;
  error: string | null;
  onRetry: () => void;
}) {
  if (state === "loading") {
    return (
      <section className={styles.stateGate} aria-live="polite">
        <LoaderCircle className={styles.spin} size={22} />
        <p>READING APPLICATION</p>
        <h1>正在连接 LAE API</h1>
        <span>读取应用、部署历史与当前登录态。</span>
      </section>
    );
  }
  const guest = state === "guest";
  const missing = state === "not-found";
  return (
    <section className={styles.stateGate}>
      {guest ? <LockKeyhole size={22} /> : missing ? <XCircle size={22} /> : <Activity size={22} />}
      <p>{guest ? "SESSION REQUIRED" : missing ? "APPLICATION NOT FOUND" : "API UNAVAILABLE"}</p>
      <h1>{guest ? "登录后查看应用" : missing ? "找不到这个应用" : "暂时无法读取详情"}</h1>
      <span>
        {guest
          ? "应用详情只接受用户登录态；deploy token 请通过 LAE CLI 使用。"
          : missing
            ? "应用不存在，或当前租户没有访问权限。"
            : error || "页面没有回退到缓存数据或模拟状态。"}
      </span>
      <div>
        {guest ? <Link href="/login">邮件登录 <ChevronRight size={13} /></Link> : <button type="button" onClick={onRetry}>重试 <RefreshCw size={13} /></button>}
        <Link href="/#applications"><ArrowLeft size={13} /> 返回应用列表</Link>
      </div>
    </section>
  );
}

function OverviewCell({ label, value, note }: { label: string; value: string; note: string }) {
  return <article><span>{label}</span><strong title={value}>{value}</strong><small>{note}</small></article>;
}

function PanelHeading({
  eyebrow,
  title,
  note,
  icon: Icon,
  id,
}: {
  eyebrow: string;
  title: string;
  note: string;
  icon: typeof Activity;
  id: string;
}) {
  return (
    <div className={styles.panelHeading}>
      <span><Icon size={13} /></span>
      <div><p>{eyebrow}</p><h2 id={id}>{title}</h2><small>{note}</small></div>
    </div>
  );
}

function StatusPill({ value }: { value: string }) {
  return <span className={`${styles.statusPill} ${statusTone(value)}`}><i />{stateLabel(value)}</span>;
}

function EmptyState({ icon: Icon, text }: { icon: typeof Activity; text: string }) {
  return <div className={styles.emptyState}><Icon size={17} /><span>{text}</span></div>;
}

function OperationInspector({
  operationId,
  operation,
  events,
  error,
  interactionLocked,
  onDeployUpdate,
}: {
  operationId: string | null;
  operation: Operation | null;
  events: OperationEvent[];
  error: string | null;
  interactionLocked: boolean;
  onDeployUpdate: (
    analysisId: string,
    confirmations: UpdateConfirmationCode[],
    note: string,
  ) => void;
}) {
  if (!operationId) {
    return <div className={styles.operationInspector}><EmptyState icon={Activity} text="选择一条部署记录查看操作事件。" /></div>;
  }
  return (
    <div className={styles.operationInspector}>
      <div className={styles.operationHeader}>
        <div>
          <span>OPERATION</span>
          <strong>{shortId(operationId)}</strong>
        </div>
        {operation ? <StatusPill value={operation.status} /> : <LoaderCircle className={styles.spin} size={14} />}
      </div>
      {operation ? (
        <dl className={styles.operationFacts}>
          <div><dt>类型</dt><dd>{operation.kind}</dd></div>
          <div><dt>阶段</dt><dd>{operation.phase || "—"}</dd></div>
          <div><dt>游标</dt><dd>{operation.cursor}</dd></div>
          <div><dt>终态</dt><dd>{operation.terminal ? "yes" : "no"}</dd></div>
        </dl>
      ) : null}
      {operation?.updateCheck ? (
        <UpdateCheckSummary
          operation={operation}
          interactionLocked={interactionLocked}
          onDeployUpdate={onDeployUpdate}
        />
      ) : null}
      {operation?.error ? <p className={styles.operationFailure}>{operation.error.code} · {operation.error.message}</p> : null}
      {error ? <p className={styles.operationFailure}>{error}</p> : null}
      <div className={styles.eventTimeline}>
        {events.length ? events.map((event) => (
          <article key={event.eventId} className={event.level === "error" ? styles.eventError : undefined}>
            <i />
            <div>
              <span>{event.phase || event.type} · {event.status}</span>
              <strong>{event.message}</strong>
              <small>{formatTimestamp(event.createdAt)} · #{event.cursor}</small>
            </div>
          </article>
        )) : <span className={styles.eventWaiting}>正在等待 operation 事件…</span>}
      </div>
    </div>
  );
}

function UpdateCheckSummary({
  operation,
  interactionLocked,
  onDeployUpdate,
}: {
  operation: Operation;
  interactionLocked: boolean;
  onDeployUpdate: (
    analysisId: string,
    confirmations: UpdateConfirmationCode[],
    note: string,
  ) => void;
}) {
  const result = operation.updateCheck;
  if (!result) return null;
  const sections = result.changes ? [
    ["服务", result.changes.services],
    ["公网路由", result.changes.routes],
    ["持久卷", result.changes.volumes],
    ["环境变量", result.changes.environment],
  ] as const : [];
  const confirmationLabels: Record<string, string> = {
    SERVICE_REMOVAL: "将移除服务",
    PUBLIC_ROUTE_CHANGE: "公网路由将变化",
    PERSISTENT_VOLUME_CHANGE: "持久卷定义将变化",
    REQUIRED_ENVIRONMENT_ADDED: "新增必填环境变量",
  };
  const confirmationNote = result.changes?.confirmations
    .map((code) => confirmationLabels[code] || code)
    .join(" · ") || "候选计划未标记破坏性变化。";
  return (
    <div className={styles.updateCheck}>
      <strong><GitCompareArrows size={12} /> 更新检查</strong>
      <div>
        <span>基线 <b>{result.baselineAvailable ? "available" : "missing"}</b></span>
        <span>源码 <b>{result.sourceChanged ? "changed" : "same"}</b></span>
        <span>计划 <b>{result.deploymentPlanChanged ? "changed" : "same"}</b></span>
        <span>结论 <b>{result.changed ? "update" : "current"}</b></span>
      </div>
      {result.changes ? (
        <div className={styles.updateDiff}>
          {sections.map(([label, changes]) => {
            const entries = [
              ...changes.added.map((value) => ({ value, kind: "added", marker: "+" })),
              ...changes.removed.map((value) => ({ value, kind: "removed", marker: "−" })),
              ...changes.changed.map((value) => ({ value, kind: "changed", marker: "~" })),
            ];
            return entries.length ? (
              <section key={label}>
                <b>{label}</b>
                <ul>
                  {entries.map((entry) => (
                    <li key={`${entry.kind}:${entry.value}`} data-kind={entry.kind}>
                      <i>{entry.marker}</i>{entry.value}
                    </li>
                  ))}
                </ul>
              </section>
            ) : null;
          })}
          {result.changes.destructive ? (
            <aside className={styles.updateWarning} role="alert">
              <strong>部署前需要明确确认</strong>
              <span>{result.changes.confirmations.map((code) => confirmationLabels[code] || code).join(" · ")}</span>
            </aside>
          ) : null}
        </div>
      ) : result.deploymentPlanChanged ? (
        <p className={styles.updateDiffUnavailable}>该历史检查没有逐项差异，请重新执行更新检查。</p>
      ) : null}
      {result.changed && result.candidateAnalysis ? (
        <button
          className={styles.updateDeployButton}
          type="button"
          disabled={
            interactionLocked ||
            result.candidateAnalysis.verdict !== "deployable" ||
            (result.deploymentPlanChanged && !result.changes)
          }
          onClick={() => onDeployUpdate(
            result.candidateAnalysis!.id,
            result.changes?.confirmations || [],
            confirmationNote,
          )}
        >
          <CloudUpload size={11} />
          {result.candidateAnalysis.verdict === "deployable"
            ? result.changes?.destructive
              ? "确认并部署此更新"
              : "部署此更新"
            : result.candidateAnalysis.verdict === "needs_input"
              ? "补齐配置后再部署"
              : "当前候选不可部署"}
        </button>
      ) : null}
    </div>
  );
}

function MetricBoard({
  metrics,
  loading,
}: {
  metrics: ApplicationMetricHistory | null;
  loading: boolean;
}) {
  const entries = metrics ? Object.entries(metrics.series) : [];
  return (
    <div className={styles.metricBoard}>
      <div className={styles.observabilityHeading}>
        <div><Gauge size={13} /><strong>METRICS</strong></div>
        <span>{metrics ? `${metrics.windowSeconds}s · ${formatTimestamp(metrics.updatedAt)}` : loading ? "loading" : "unavailable"}</span>
      </div>
      {entries.length ? (
        <div className={styles.metricGrid}>
          {entries.map(([name, points]) => <MetricCard key={name} name={name} points={points} />)}
        </div>
      ) : (
        <EmptyState icon={Gauge} text={loading ? "正在读取指标…" : "当前服务没有返回指标序列。"} />
      )}
    </div>
  );
}

function MetricCard({ name, points }: { name: string; points: Array<[number, number]> }) {
  const valid = points.filter((point) => Number.isFinite(point[0]) && Number.isFinite(point[1]));
  const values = valid.map((point) => point[1]);
  const minimum = values.length ? Math.min(...values) : 0;
  const maximum = values.length ? Math.max(...values) : 0;
  const width = 240;
  const height = 58;
  const range = maximum - minimum || 1;
  const path = valid.map((point, index) => {
    const x = valid.length <= 1 ? 0 : (index / (valid.length - 1)) * width;
    const y = height - ((point[1] - minimum) / range) * (height - 8) - 4;
    return `${index === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  const latest = valid.at(-1)?.[1];
  return (
    <article className={styles.metricCard}>
      <div><span>{name}</span><strong>{latest === undefined ? "—" : formatMetric(latest)}</strong></div>
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${name} 趋势`} preserveAspectRatio="none">
        <path d={path || `M0,${height / 2} L${width},${height / 2}`} />
      </svg>
      <small>{valid.length} points · min {formatMetric(minimum)} · max {formatMetric(maximum)}</small>
    </article>
  );
}

function LogTail({ logs, loading }: { logs: ApplicationLogTail | null; loading: boolean }) {
  return (
    <div className={styles.logTail}>
      <div className={styles.observabilityHeading}>
        <div><SquareTerminal size={13} /><strong>LOG TAIL</strong></div>
        <span>{logs ? `${logs.logs.length}/${logs.tail} · ${formatTimestamp(logs.updatedAt)}` : loading ? "loading" : "unavailable"}</span>
      </div>
      {logs?.logs.length ? (
        <pre>{logs.logs.map((line, index) => <span key={`${index}:${line.slice(0, 32)}`}><i>{String(index + 1).padStart(3, "0")}</i>{line}</span>)}</pre>
      ) : (
        <EmptyState icon={SquareTerminal} text={loading ? "正在读取日志…" : "当前服务没有返回日志。"} />
      )}
      {logs?.truncated ? <small className={styles.truncated}>输出已截断，只显示最后 {logs.tail} 行。</small> : null}
    </div>
  );
}

function ConfirmationDialog({
  value,
  busy,
  onClose,
  onConfirm,
}: {
  value: Confirmation;
  busy: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  return (
    <div className={styles.dialogBackdrop} role="presentation" onMouseDown={onClose}>
      <section
        className={styles.dialog}
        role="dialog"
        aria-modal="true"
        aria-labelledby="application-confirmation-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <span>{value.action === "rollback" ? <RotateCcw size={17} /> : value.action === "deploy-update" ? <CloudUpload size={17} /> : <Pause size={17} />}</span>
        <p>CONFIRM RUNTIME ACTION</p>
        <h2 id="application-confirmation-title">{value.title}</h2>
        <small>{value.note}</small>
        <div>
          <button type="button" onClick={onClose} disabled={busy}>取消</button>
          <button className={styles.dangerButton} type="button" onClick={onConfirm} disabled={busy}>
            {busy ? <LoaderCircle className={styles.spin} size={13} /> : value.action === "rollback" ? <Undo2 size={13} /> : value.action === "deploy-update" ? <CloudUpload size={13} /> : <Pause size={13} />}
            {busy ? "正在提交" : "确认执行"}
          </button>
        </div>
      </section>
    </div>
  );
}

function stateLabel(value: string) {
  const labels: Record<string, string> = {
    running: "运行中",
    provisioning: "准备中",
    degraded: "已降级",
    failed: "失败",
    suspending: "停止中",
    suspended: "已停止",
    deleted: "已删除",
    unknown: "未知",
    succeeded: "成功",
    pending: "等待中",
    queued: "排队中",
    in_progress: "执行中",
    active: "生效中",
    ready: "就绪",
    healthy: "健康",
  };
  return labels[value.toLowerCase()] || value;
}

function statusTone(value: string) {
  const normalized = value.toLowerCase();
  if (["running", "succeeded", "ready", "active", "healthy"].includes(normalized)) return styles.toneGood;
  if (["failed", "error", "unhealthy", "deleted"].includes(normalized)) return styles.toneBad;
  if (["degraded", "suspending", "provisioning", "pending", "queued", "in_progress"].includes(normalized)) return styles.toneWarn;
  return styles.toneMuted;
}

function actionStartMessage(action: ApplicationAction) {
  const messages: Record<ApplicationAction, string> = {
    "check-update": "更新检查已启动；完成后会显示源码和部署计划是否变化。",
    suspend: "停止操作已启动。",
    resume: "恢复操作已启动。",
    restart: "重启操作已启动。",
    rollback: "回滚操作已启动。",
    delete: "删除操作已启动。",
  };
  return messages[action];
}

function apiErrorMessage(error: unknown, fallback: string) {
  if (error instanceof LaeApiError) return `${error.message}（${error.code}）`;
  return fallback;
}

function shortId(value: string | null | undefined) {
  if (!value) return "—";
  return value.length > 13 ? `${value.slice(0, 8)}…${value.slice(-4)}` : value;
}

function shortDigest(value: string | null | undefined) {
  if (!value) return "image pending";
  const digest = value.includes(":") ? value.split(":").at(-1) || value : value;
  return `sha · ${digest.slice(0, 12)}`;
}

function formatTimestamp(value: string | null | undefined) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function formatMetric(value: number) {
  const absolute = Math.abs(value);
  if (absolute >= 1_000_000_000) return `${(value / 1_000_000_000).toFixed(2)}G`;
  if (absolute >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`;
  if (absolute >= 1_000) return `${(value / 1_000).toFixed(2)}K`;
  if (absolute > 0 && absolute < 0.01) return value.toExponential(2);
  return value.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}
