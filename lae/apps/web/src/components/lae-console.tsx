"use client";

import {
  Activity,
  ArrowRight,
  Boxes,
  Check,
  ChevronRight,
  CircleUserRound,
  CloudUpload,
  Command,
  Copy,
  ExternalLink,
  FileArchive,
  GitFork,
  Globe2,
  Layers3,
  LockKeyhole,
  Orbit,
  Pause,
  Play,
  RefreshCw,
  RotateCcw,
  ServerCog,
  Sparkles,
  SquareTerminal,
  Sun,
  Moon,
  Trash2,
  Undo2,
  X,
  type LucideIcon,
} from "lucide-react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import Link from "next/link";
import { FormEvent, useEffect, useId, useRef, useState } from "react";

import {
  createApplication,
  createDeployment,
  createGitAnalysis,
  createSourceConnection,
  createStaticUpload,
  createUploadAnalysis,
  completeStaticUpload,
  getApplication,
  getApplicationLogs,
  getApplicationMetrics,
  getAnalysis,
  getDeploymentConfiguration,
  getOperation,
  getOperationEvents,
  getPrincipal,
  getStaticUpload,
  LaeApiError,
  launchTemplate,
  listOperations,
  listApplications,
  listApplicationDeployments,
  listSourceConnections,
  listTemplates,
  newIdempotencyKey,
  patchPlanEnvironment,
  recoverOperation,
  requestApplicationAction,
  sha256File,
  staticUploadMediaType,
  transferStaticUpload,
  type LaePrincipal,
  type DeploymentConfiguration,
  type OperationEvent,
  type OperationListItem,
  type SourceConnection,
  type ApplicationAction,
  type ApplicationRecord,
  type ApplicationLogTail,
  type ApplicationMetricHistory,
  type ApplicationTemplate,
  type Analysis,
} from "../lib/lae-api";

type FlowState = "idle" | "configuring" | "diagnosing" | "ready" | "deploying" | "live";
type SourceKind = "github" | "git" | "file" | "template";

type Template = Omit<ApplicationTemplate, "icon"> & {
  iconName: string;
  icon: LucideIcon;
  x: number;
  y: number;
  drift: number;
};

const templateIcons: Record<string, LucideIcon> = {
  orbit: Orbit,
  "server-cog": ServerCog,
  layers: Layers3,
  "square-terminal": SquareTerminal,
};

function analysisFailureMessage(analysis: Analysis, subject: string): string {
  if (analysis.verdict === "diagnostic_failed") {
    return `平台诊断暂时失败（${analysis.diagnostic.code || "LAE_DIAGNOSTIC_FAILED"}）。${subject}没有被判定为不支持，请稍后重试。`;
  }
  if (analysis.verdict === "unsupported") {
    const fixes = analysis.blockers
      .map((item) => `${item.code} · ${item.path} · ${item.field}：${item.remediation}`)
      .join("；");
    return fixes
      ? `${subject}暂不支持部署。${fixes}`
      : `${subject}暂不支持部署；请重新运行诊断以获取完整修复建议。`;
  }
  return `${subject}目前不能进入部署，请按诊断结果补齐配置后重试。`;
}

const templatePositions: Record<string, { x: number; y: number; drift: number }> = {
  "nextjs-docker": { x: 18, y: 27, drift: 0.2 },
  "fastapi-minimal": { x: 43, y: 16, drift: 0.8 },
  "flask-hello": { x: 69, y: 32, drift: 1.3 },
  "express-hello": { x: 48, y: 68, drift: 0.5 },
};

const templateDescriptions: Record<string, string> = {
  "nextjs-docker": "已配置 standalone 输出的 Next.js App Router 起步应用。",
  "fastapi-minimal": "轻量 Python API，并自带自动生成的 OpenAPI 文档界面。",
  "flask-hello": "克制、易于扩展的 Python Web 服务起步应用。",
  "express-hello": "使用标准启动命令的轻量 Node.js HTTP 服务。",
};

type ConsoleSection = "deployment" | "applications" | "activity" | "cli";
type CatalogStatus = "checking" | "connected" | "unavailable";

const consoleSections: ConsoleSection[] = ["deployment", "applications", "activity", "cli"];

const sectionCopy: Record<
  ConsoleSection,
  { eyebrow: string; title: string; note: string; breadcrumb: string }
> = {
  deployment: {
    eyebrow: "APPLICATION DELIVERY",
    title: "部署应用",
    note: "LAE Agent 诊断源码并生成 Luma 部署计划；支持 Git、静态产物与多服务 Compose。",
    breadcrumb: "部署",
  },
  applications: {
    eyebrow: "APPLICATIONS",
    title: "应用",
    note: "查看运行状态、公开域名和服务拓扑，并执行暂停、重启、更新检查与回滚。",
    breadcrumb: "应用",
  },
  activity: {
    eyebrow: "OPERATIONS",
    title: "部署活动",
    note: "诊断、构建和部署都保留结构化进度；中断的操作可以从这里继续查看或恢复。",
    breadcrumb: "活动",
  },
  cli: {
    eyebrow: "AGENT INTERFACE",
    title: "LAE CLI",
    note: "为用户自己的 Agent 提供与控制台等价、机器可读的诊断与部署协议。",
    breadcrumb: "CLI",
  },
};

function arrangeTemplates(items: ApplicationTemplate[]): Template[] {
  return items.map(({ icon, ...item }, index) => ({
    ...item,
    description: templateDescriptions[item.id] || item.description,
    iconName: icon,
    icon: templateIcons[icon] || Sparkles,
    ...(templatePositions[item.id] || {
      x: 18 + (index % 3) * 27,
      y: 28 + (index % 2) * 38,
      drift: (index % 4) * 0.35,
    }),
  }));
}

type ShoreApplication = {
  id: string;
  name: string;
  domain: string;
  status: string;
  services: number;
  tone: "healthy" | "paused" | "pending" | "degraded" | "failed";
  desiredState: "running" | "suspended" | "deleted";
  lifecycleEnabled: boolean;
  rollbackDeploymentId: string | null;
};

type ConfirmedLifecycleAction = {
  application: ShoreApplication;
  action: "rollback" | "delete";
};

type LiveDeployment = {
  application: ApplicationRecord | null;
  deploymentId: string;
  operationId: string;
};

type GitSourceInput = {
  name: string;
  slug: string;
  repository: string;
  ref: string;
  subdirectory?: string;
  connectionId?: string;
};

const stateCopy: Record<FlowState, { eyebrow: string; title: string; note: string }> = {
  idle: { eyebrow: "NEW DEPLOYMENT", title: "从哪里开始？", note: "选择源码，LAE Agent 会先判断它是否适合部署。" },
  configuring: { eyebrow: "SOURCE · 01", title: "给应用一个起点", note: "来源信息只用于创建受租户隔离的诊断任务。" },
  diagnosing: { eyebrow: "LAE AGENT · 02", title: "正在读懂这个应用", note: "源码仅在隔离的 Luma Builder 中展开与分析。" },
  ready: { eyebrow: "READY · 03", title: "可以部署", note: "拓扑与端口已确认。部署文件将由 LAE 保存，无需写回仓库。" },
  deploying: { eyebrow: "DEPLOYING · 04", title: "正在部署服务", note: "构建镜像、分配路由并逐个验证 HTTP 服务。" },
  live: { eyebrow: "LIVE · 05", title: "部署完成", note: "应用已进入列表，域名在更新与重启时保持稳定。" },
};

export function LaeConsole() {
  const reduceMotion = useReducedMotion();
  const [theme, setTheme] = useState<"light" | "dark">("dark");
  const [selectedTemplate, setSelectedTemplate] = useState<Template | null>(null);
  const [templates, setTemplates] = useState<Template[]>([]);
  const [templatesLoading, setTemplatesLoading] = useState(true);
  const [templatesNotice, setTemplatesNotice] = useState<string | null>(null);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [source, setSource] = useState<SourceKind | null>(null);
  const [flow, setFlow] = useState<FlowState>("idle");
  const [principal, setPrincipal] = useState<LaePrincipal | null>(null);
  const [shoreApplications, setShoreApplications] = useState<ShoreApplication[]>([]);
  const [catalogLoading, setCatalogLoading] = useState(true);
  const [catalogStatus, setCatalogStatus] = useState<CatalogStatus>("checking");
  const [catalogNotice, setCatalogNotice] = useState<string | null>(null);
  const [catalogRefresh, setCatalogRefresh] = useState(0);
  const [lifecycleBusy, setLifecycleBusy] = useState<Set<string>>(() => new Set());
  const [confirmedLifecycleAction, setConfirmedLifecycleAction] =
    useState<ConfirmedLifecycleAction | null>(null);
  const [observingApplication, setObservingApplication] =
    useState<ShoreApplication | null>(null);
  const [applicationLogs, setApplicationLogs] =
    useState<ApplicationLogTail | null>(null);
  const [applicationMetrics, setApplicationMetrics] =
    useState<ApplicationMetricHistory | null>(null);
  const [observabilityLoading, setObservabilityLoading] = useState(false);
  const [observabilityNotice, setObservabilityNotice] = useState<string | null>(null);
  const [flowError, setFlowError] = useState<string | null>(null);
  const [operationEvents, setOperationEvents] = useState<OperationEvent[]>([]);
  const [recentOperations, setRecentOperations] = useState<OperationListItem[]>([]);
  const [recoveryBusy, setRecoveryBusy] = useState<string | null>(null);
  const [deploymentConfiguration, setDeploymentConfiguration] =
    useState<DeploymentConfiguration | null>(null);
  const [deploymentPlan, setDeploymentPlan] =
    useState<DeploymentConfiguration | null>(null);
  const [liveDeployment, setLiveDeployment] = useState<LiveDeployment | null>(null);
  const [environmentSaving, setEnvironmentSaving] = useState(false);
  const [activeSection, setActiveSection] = useState<ConsoleSection>("deployment");
  const [activeRun, setActiveRun] = useState<{
    applicationId: string;
    analysisId: string;
    environmentVersion: number;
  } | null>(null);
  const runController = useRef<AbortController | null>(null);
  const observabilityController = useRef<AbortController | null>(null);
  const recoveredOnLoad = useRef(false);
  const fileInputId = useId();

  useEffect(() => {
    const stored = window.localStorage.getItem("luma.dashboard.theme");
    const next =
      stored === "light" || stored === "dark"
        ? stored
        : window.matchMedia("(prefers-color-scheme: light)").matches
          ? "light"
          : "dark";
    setTheme(next);
    document.documentElement.dataset.theme = next;
  }, []);

  const toggleTheme = () => {
    const next = theme === "dark" ? "light" : "dark";
    setTheme(next);
    document.documentElement.dataset.theme = next;
    window.localStorage.setItem("luma.dashboard.theme", next);
  };

  const stopRun = () => {
    runController.current?.abort();
    runController.current = null;
  };

  useEffect(
    () => () => {
      stopRun();
      observabilityController.current?.abort();
    },
    [],
  );

  useEffect(() => {
    const setSectionFromHash = () => {
      const section = window.location.hash.slice(1) as ConsoleSection;
      setActiveSection(consoleSections.includes(section) ? section : "deployment");
    };
    setSectionFromHash();
    window.addEventListener("hashchange", setSectionFromHash);
    return () => window.removeEventListener("hashchange", setSectionFromHash);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setTemplatesLoading(true);
    setTemplatesNotice(null);
    void (async () => {
      try {
        const catalog = await listTemplates(controller.signal);
        if (controller.signal.aborted) return;
        const arranged = arrangeTemplates(catalog.templates);
        setTemplates(arranged);
        setSelectedTemplate((current) =>
          current ? arranged.find((item) => item.id === current.id) || null : current,
        );
      } catch {
        if (controller.signal.aborted) return;
        setTemplates([]);
        setSelectedTemplate(null);
        setTemplatesNotice("模板目录暂时不可用；不会用演示数据替代真实模板。");
      } finally {
        if (!controller.signal.aborted) setTemplatesLoading(false);
      }
    })();
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setCatalogLoading(true);
    setCatalogStatus("checking");
    setCatalogNotice(null);
    void (async () => {
      try {
        const identity = await getPrincipal(controller.signal);
        const [catalog, operationPage] = await Promise.all([
          listApplications(controller.signal),
          listOperations({ limit: 12 }, controller.signal).catch(() => ({
            operations: [],
            hasMore: false,
            nextCursor: null,
          })),
        ]);
        const details = await Promise.all(
          catalog.applications.map(async (application) => {
            try {
              const [detail, deploymentHistory] = await Promise.all([
                getApplication(application.id, controller.signal),
                listApplicationDeployments(
                  application.id,
                  8,
                  controller.signal,
                ).catch(() => ({ deployments: [] })),
              ]);
              return { detail, deploymentHistory };
            } catch {
              return null;
            }
          }),
        );
        if (controller.signal.aborted) return;
        setCatalogStatus("connected");
        setPrincipal(identity);
        setRecentOperations(operationPage.operations);
        setShoreApplications(
          catalog.applications.map((application, index) => {
            const view = details[index];
            const detail = view?.detail;
            const primary = detail?.routes.find((route) => route.primary);
            const currentDeployment = view?.deploymentHistory.deployments.find(
              (deployment) => deployment.id === application.currentDeploymentId,
            );
            const rollbackCandidate = currentDeployment?.previousDeploymentId
              ? view?.deploymentHistory.deployments.find(
                  (deployment) =>
                    deployment.id === currentDeployment.previousDeploymentId &&
                    deployment.status === "succeeded",
                )
              : null;
            const suspended = application.desiredState === "suspended";
            const observed = application.observedState;
            const running = observed === "running";
            const degraded = observed === "degraded";
            const failed = observed === "failed";
            return {
              id: application.id,
              name: application.name,
              domain: primary?.hostname || "等待首次部署",
              status: suspended
                ? "已暂停"
                : running
                  ? "运行中"
                  : degraded
                    ? "运行降级"
                    : failed
                      ? "运行失败"
                  : application.kind === "pending"
                    ? "待诊断"
                    : observed === "provisioning"
                      ? "正在启动"
                      : observed === "suspending"
                        ? "正在暂停"
                        : "状态未知",
              services: detail?.services.length || 0,
              tone: suspended
                ? "paused"
                : running
                  ? "healthy"
                  : degraded
                    ? "degraded"
                    : failed
                      ? "failed"
                      : "pending",
              desiredState: application.desiredState,
              lifecycleEnabled:
                application.currentDeploymentId !== null && application.kind !== "pending",
              rollbackDeploymentId: rollbackCandidate?.id || null,
            };
          }),
        );
      } catch (error) {
        if (controller.signal.aborted) return;
        const unauthenticated = error instanceof LaeApiError && error.status === 401;
        setCatalogStatus(unauthenticated ? "connected" : "unavailable");
        setPrincipal(null);
        setShoreApplications([]);
        setRecentOperations([]);
        setCatalogNotice(
          unauthenticated
            ? "登录后，这里会显示你的真实应用与稳定域名。"
            : "暂时无法读取应用目录；部署入口不会用示例数据冒充真实状态。",
        );
      } finally {
        if (!controller.signal.aborted) setCatalogLoading(false);
      }
    })();
    return () => controller.abort();
  }, [catalogRefresh]);

  const selectSource = (nextSource: SourceKind) => {
    stopRun();
    setSource(nextSource);
    setFlowError(null);
    setOperationEvents([]);
    setDeploymentConfiguration(null);
    setDeploymentPlan(null);
    setLiveDeployment(null);
    setFlow("configuring");
  };

  const selectFile = (file: File | null) => {
    if (!file) return;
    setSelectedFile(file);
    selectSource("file");
  };

  const watchOperation = async (operationId: string, signal: AbortSignal) => {
    let cursor = 0;
    while (!signal.aborted) {
      const page = await getOperationEvents(operationId, cursor, signal);
      cursor = Math.max(cursor, page.cursor);
      if (page.events.length) {
        setOperationEvents((current) => {
          const byId = new Map(current.map((event) => [event.eventId, event]));
          page.events.forEach((event) => byId.set(event.eventId, event));
          return [...byId.values()].sort((left, right) => left.cursor - right.cursor).slice(-12);
        });
      }
      if (page.terminal) return page.status;
      await abortableDelay(850, signal);
    }
    throw new DOMException("Operation watch canceled", "AbortError");
  };

  const recoverHistoricalOperation = async (operation: OperationListItem) => {
    if (recoveryBusy) return;
    stopRun();
    const controller = new AbortController();
    runController.current = controller;
    setRecoveryBusy(operation.id);
    setFlowError(null);
    try {
      const recovered = await recoverOperation(operation.id, {}, controller.signal);
      setOperationEvents(recovered.events.slice(-12));
      let status = recovered.operation.status;
      if (!recovered.terminal) {
        setFlow(operation.kind === "deployment.create" ? "deploying" : "diagnosing");
        status = await watchOperation(operation.id, controller.signal);
      }
      if (
        operation.kind === "deployment.create" &&
        status === "succeeded" &&
        operation.applicationId
      ) {
        const application = await getApplication(operation.applicationId, controller.signal);
        const deploymentId = application.application.currentDeploymentId;
        if (deploymentId) {
          setLiveDeployment({ application, deploymentId, operationId: operation.id });
          setFlow("live");
        } else {
          setFlow("idle");
        }
      } else {
        setFlow("idle");
        if (status !== "succeeded") {
          setFlowError("该操作已结束但未成功；历史事件已恢复，现有健康版本未被替换。");
        }
      }
      setCatalogRefresh((value) => value + 1);
    } catch (error) {
      if (!controller.signal.aborted) {
        setFlow("idle");
        setFlowError(
          error instanceof LaeApiError ? error.message : "操作历史暂时无法恢复。",
        );
      }
    } finally {
      if (runController.current === controller) runController.current = null;
      setRecoveryBusy(null);
    }
  };

  useEffect(() => {
    if (recoveredOnLoad.current || flow !== "idle") return;
    const active = recentOperations.find(
      (operation) => operation.kind === "deployment.create" && !operation.terminal,
    );
    if (!active) return;
    recoveredOnLoad.current = true;
    void recoverHistoricalOperation(active);
  }, [flow, recentOperations]);

  const runLifecycleAction = async (
    application: ShoreApplication,
    action: ApplicationAction,
    input: { deploymentId?: string } = {},
  ) => {
    if (lifecycleBusy.has(application.id)) return;
    const key = `${application.id}:${action}`;
    setLifecycleBusy((current) => new Set(current).add(application.id));
    setCatalogNotice(null);
    try {
      const result = await requestApplicationAction(
        application.id,
        action,
        input,
        newIdempotencyKey(key),
      );
      setCatalogNotice(
        action === "check-update"
          ? "更新检查已交给 LAE Agent，当前版本会继续运行。"
          : `${application.name} 的${lifecycleActionLabel(action)}请求已进入队列。`,
      );
      setCatalogRefresh((value) => value + 1);
      const controller = new AbortController();
      void watchOperation(result.operation.id, controller.signal)
        .then(async (status) => {
          if (action !== "check-update" || status !== "succeeded") return;
          const operation = await getOperation(result.operation.id, controller.signal);
          const comparison = operation.updateCheck;
          if (!comparison) {
            setCatalogNotice("更新检查已完成，但比较结果暂时不可用；当前版本未受影响。");
          } else if (!comparison.baselineAvailable) {
            setCatalogNotice("更新检查完成：当前应用没有可比较的已部署基线，候选部署计划已生成，发布前需要人工确认。");
          } else if (!comparison.changed) {
            setCatalogNotice("更新检查完成：源代码与部署计划均未变化，当前版本无需更新。");
          } else if (comparison.sourceChanged && comparison.deploymentPlanChanged) {
            setCatalogNotice("更新检查完成：源代码与部署计划均有变化；当前健康版本继续运行，确认配置后再发布候选版本。");
          } else if (comparison.deploymentPlanChanged) {
            setCatalogNotice("更新检查完成：部署计划发生变化；当前健康版本继续运行，请重点复核服务、路由、环境变量与卷。");
          } else {
            setCatalogNotice("更新检查完成：源代码有变化，但部署计划结构未变；当前健康版本继续运行。");
          }
        })
        .catch(() => {
          if (action === "check-update") {
            setCatalogNotice("更新检查未能完成，当前健康版本未受影响，可稍后重试。");
          }
        })
        .finally(() => setCatalogRefresh((value) => value + 1));
    } catch (error) {
      setCatalogNotice(
        error instanceof LaeApiError
          ? error.message
          : "应用操作暂时无法提交，请稍后重试。",
      );
    } finally {
      setLifecycleBusy((current) => {
        const next = new Set(current);
        next.delete(application.id);
        return next;
      });
    }
  };

  const inspectApplication = async (application: ShoreApplication) => {
    observabilityController.current?.abort();
    const controller = new AbortController();
    observabilityController.current = controller;
    setObservingApplication(application);
    setApplicationLogs(null);
    setApplicationMetrics(null);
    setObservabilityNotice(null);
    setObservabilityLoading(true);
    try {
      const [logs, metrics] = await Promise.all([
        getApplicationLogs(application.id, { tail: 120 }, controller.signal),
        getApplicationMetrics(application.id, { window: 3600 }, controller.signal),
      ]);
      if (controller.signal.aborted) return;
      setApplicationLogs(logs);
      setApplicationMetrics(metrics);
    } catch (error) {
      if (controller.signal.aborted) return;
      setObservabilityNotice(
        error instanceof LaeApiError
          ? error.message
          : "运行观测暂时不可用；应用本身不会因此受到影响。",
      );
    } finally {
      if (!controller.signal.aborted) setObservabilityLoading(false);
    }
  };

  const closeObservability = () => {
    observabilityController.current?.abort();
    observabilityController.current = null;
    setObservingApplication(null);
  };

  const beginGitDiagnosis = async (input: GitSourceInput) => {
    if (!principal) {
      setFlowError("请先注册或登录，再创建属于你的应用。");
      return;
    }
    stopRun();
    const controller = new AbortController();
    runController.current = controller;
    setFlowError(null);
    setOperationEvents([]);
    setFlow("diagnosing");
    try {
      const created = await createApplication(
        { name: input.name, slug: input.slug },
        newIdempotencyKey("app-create"),
        controller.signal,
      );
      const analysis = await createGitAnalysis(
        {
          applicationId: created.application.id,
          repository: input.repository,
          ref: input.ref,
          subdirectory: input.subdirectory,
          connectionId: input.connectionId,
        },
        newIdempotencyKey("analysis"),
        controller.signal,
      );
      setActiveRun({
        applicationId: created.application.id,
        analysisId: analysis.analysis.id,
        environmentVersion: created.application.environmentVersion,
      });
      const operationStatus = await watchOperation(
        analysis.operation.id,
        controller.signal,
      );
      const result = await getAnalysis(analysis.analysis.id, controller.signal);
      const configuration =
        operationStatus === "succeeded" &&
        result.planStored &&
        (result.verdict === "deployable" || result.verdict === "needs_input")
          ? await getDeploymentConfiguration(
              created.application.id,
              analysis.analysis.id,
              controller.signal,
            )
          : null;
      setDeploymentPlan(configuration?.configuration || null);
      if (
        operationStatus === "succeeded" &&
        result.verdict === "needs_input" &&
        result.planStored &&
        configuration
      ) {
        setDeploymentConfiguration(configuration.configuration);
        setFlow("configuring");
        setCatalogRefresh((value) => value + 1);
        return;
      }
      if (
        operationStatus !== "succeeded" ||
        result.verdict !== "deployable" ||
        !result.planStored
      ) {
        setFlow("configuring");
        setFlowError(
          analysisFailureMessage(result, "该来源"),
        );
        return;
      }
      setFlow("ready");
      setCatalogRefresh((value) => value + 1);
    } catch (error) {
      if (controller.signal.aborted) return;
      setFlow("configuring");
      setFlowError(
        error instanceof LaeApiError ? error.message : "诊断请求未能完成，请稍后重试。",
      );
    } finally {
      if (runController.current === controller) runController.current = null;
    }
  };

  const beginUploadDiagnosis = async (input: {
    name: string;
    slug: string;
    file: File;
  }) => {
    if (!principal) {
      setFlowError("请先注册或登录，再创建属于你的应用。");
      return;
    }
    stopRun();
    const controller = new AbortController();
    runController.current = controller;
    setFlowError(null);
    setOperationEvents([]);
    setFlow("diagnosing");
    try {
      const mediaType = staticUploadMediaType(input.file.name);
      const digest = await sha256File(input.file);
      const created = await createApplication(
        { name: input.name, slug: input.slug },
        newIdempotencyKey("app-create"),
        controller.signal,
      );
      const reserved = await createStaticUpload(
        {
          applicationId: created.application.id,
          filename: input.file.name,
          mediaType,
          sizeBytes: input.file.size,
          sha256: digest,
        },
        newIdempotencyKey("upload-create"),
        controller.signal,
      );
      if (!reserved.uploadUrlIssued || !reserved.transfer) {
        throw new LaeApiError({ code: "LAE_UPLOAD_GRANT_MISSING", status: 502 });
      }
      await transferStaticUpload(input.file, reserved.transfer, controller.signal);
      await completeStaticUpload(
        reserved.upload.id,
        newIdempotencyKey("upload-complete"),
        controller.signal,
      );

      const scanDeadline = Date.now() + 3 * 60_000;
      let upload = reserved.upload;
      while (!controller.signal.aborted && Date.now() < scanDeadline) {
        const status = await getStaticUpload(reserved.upload.id, controller.signal);
        upload = status.upload;
        if (upload.status === "ready") break;
        if (["failed", "deleted", "expired"].includes(upload.status)) {
          throw new LaeApiError({
            code: upload.failureCode || "LAE_UPLOAD_SCAN_FAILED",
            status: 422,
          });
        }
        await abortableDelay(850, controller.signal);
      }
      if (upload.status !== "ready") {
        throw new LaeApiError({ code: "LAE_UPLOAD_SCAN_TIMED_OUT", status: 503, retryable: true });
      }

      const analysis = await createUploadAnalysis(
        {
          applicationId: created.application.id,
          uploadId: upload.id,
        },
        newIdempotencyKey("analysis"),
        controller.signal,
      );
      setActiveRun({
        applicationId: created.application.id,
        analysisId: analysis.analysis.id,
        environmentVersion: created.application.environmentVersion,
      });
      const operationStatus = await watchOperation(
        analysis.operation.id,
        controller.signal,
      );
      const result = await getAnalysis(analysis.analysis.id, controller.signal);
      const configuration =
        operationStatus === "succeeded" &&
        result.planStored &&
        (result.verdict === "deployable" || result.verdict === "needs_input")
          ? await getDeploymentConfiguration(
              created.application.id,
              analysis.analysis.id,
              controller.signal,
            )
          : null;
      setDeploymentPlan(configuration?.configuration || null);
      if (
        operationStatus === "succeeded" &&
        result.verdict === "needs_input" &&
        result.planStored &&
        configuration
      ) {
        setDeploymentConfiguration(configuration.configuration);
        setFlow("configuring");
        setCatalogRefresh((value) => value + 1);
        return;
      }
      if (
        operationStatus !== "succeeded" ||
        result.verdict !== "deployable" ||
        !result.planStored
      ) {
        setFlow("configuring");
        setFlowError(
          analysisFailureMessage(result, "该静态产物"),
        );
        return;
      }
      setFlow("ready");
      setCatalogRefresh((value) => value + 1);
    } catch (error) {
      if (controller.signal.aborted) return;
      setFlow("configuring");
      setFlowError(
        error instanceof LaeApiError ? error.message : "静态产物诊断未能完成，请稍后重试。",
      );
    } finally {
      if (runController.current === controller) runController.current = null;
    }
  };

  const beginTemplateDiagnosis = async (template: Template) => {
    setSelectedTemplate(template);
    setSource("template");
    if (!principal) {
      setFlowError("请先注册或登录，再从模板创建属于你的应用。");
      return;
    }
    stopRun();
    const controller = new AbortController();
    runController.current = controller;
    setFlowError(null);
    setOperationEvents([]);
    setDeploymentConfiguration(null);
    setDeploymentPlan(null);
    setFlow("diagnosing");
    try {
      const result = await launchTemplate(
        template.id,
        {
          name: `${template.name} Application`,
          slug: `${template.id}-${crypto.randomUUID().replaceAll("-", "").slice(0, 8)}`,
        },
        newIdempotencyKey("template-launch"),
        controller.signal,
      );
      setActiveRun({
        applicationId: result.application.id,
        analysisId: result.analysis.id,
        environmentVersion: result.application.environmentVersion,
      });
      const operationStatus = await watchOperation(result.operation.id, controller.signal);
      const analysis = await getAnalysis(result.analysis.id, controller.signal);
      const configuration =
        operationStatus === "succeeded" &&
        analysis.planStored &&
        (analysis.verdict === "deployable" || analysis.verdict === "needs_input")
          ? await getDeploymentConfiguration(
              result.application.id,
              result.analysis.id,
              controller.signal,
            )
          : null;
      setDeploymentPlan(configuration?.configuration || null);
      if (
        operationStatus === "succeeded" &&
        analysis.verdict === "needs_input" &&
        analysis.planStored &&
        configuration
      ) {
        setDeploymentConfiguration(configuration.configuration);
        setFlow("configuring");
        setCatalogRefresh((value) => value + 1);
        return;
      }
      if (
        operationStatus !== "succeeded" ||
        analysis.verdict !== "deployable" ||
        !analysis.planStored
      ) {
        setFlow("idle");
        setFlowError(analysisFailureMessage(analysis, "该模板"));
        return;
      }
      setFlow("ready");
      setCatalogRefresh((value) => value + 1);
    } catch (error) {
      if (controller.signal.aborted) return;
      setFlow("idle");
      setFlowError(
        error instanceof LaeApiError ? error.message : "模板诊断未能完成，请稍后重试。",
      );
    } finally {
      if (runController.current === controller) runController.current = null;
    }
  };

  const saveEnvironment = async (
    values: Record<string, { value: string }>,
  ) => {
    if (!activeRun || !deploymentConfiguration || environmentSaving) return;
    stopRun();
    const controller = new AbortController();
    runController.current = controller;
    setEnvironmentSaving(true);
    setFlowError(null);
    try {
      const result = await patchPlanEnvironment(
        activeRun.applicationId,
        activeRun.analysisId,
        {
          expectedVersion: activeRun.environmentVersion,
          environmentSchemaDigest: deploymentConfiguration.environmentSchemaDigest,
          set: values,
        },
        newIdempotencyKey("environment"),
        controller.signal,
      );
      setActiveRun((current) =>
        current
          ? { ...current, environmentVersion: result.environment.version }
          : current,
      );
      setDeploymentConfiguration(null);
      setFlow("ready");
      setCatalogRefresh((value) => value + 1);
    } catch (error) {
      if (controller.signal.aborted) return;
      setFlowError(
        error instanceof LaeApiError
          ? error.message
          : "环境变量未能保存，请稍后重试。",
      );
    } finally {
      setEnvironmentSaving(false);
      if (runController.current === controller) runController.current = null;
    }
  };

  const deploy = async () => {
    if (!activeRun) return;
    stopRun();
    const controller = new AbortController();
    runController.current = controller;
    setFlowError(null);
    setOperationEvents([]);
    setDeploymentConfiguration(null);
    setEnvironmentSaving(false);
    setFlow("deploying");
    try {
      const created = await createDeployment(
        activeRun,
        newIdempotencyKey("deployment"),
        controller.signal,
      );
      const status = await watchOperation(created.operation.id, controller.signal);
      if (status !== "succeeded") {
        setFlow("ready");
        setFlowError("部署未通过运行态验证，现有健康版本没有被替换。");
        return;
      }
      const application = await getApplication(
        activeRun.applicationId,
        controller.signal,
      ).catch(() => null);
      setLiveDeployment({
        application,
        deploymentId: created.deployment.id,
        operationId: created.operation.id,
      });
      setFlow("live");
      setCatalogRefresh((value) => value + 1);
    } catch (error) {
      if (controller.signal.aborted) return;
      setFlow("ready");
      setFlowError(
        error instanceof LaeApiError ? error.message : "部署请求未能完成，请稍后重试。",
      );
    } finally {
      if (runController.current === controller) runController.current = null;
    }
  };

  const resetFlow = () => {
    stopRun();
    setFlow("idle");
    setSource(null);
    setSelectedFile(null);
    setActiveRun(null);
    setOperationEvents([]);
    setDeploymentConfiguration(null);
    setDeploymentPlan(null);
    setLiveDeployment(null);
    setEnvironmentSaving(false);
    setFlowError(null);
  };

  const activeCopy = sectionCopy[activeSection];

  return (
    <main className="console-shell">
      <aside className="rail" aria-label="主导航">
        <Link className="rail-brand" href="/" aria-label="Luma Application Engine">
          <div className="brand-mark"><span /><span /><span /></div>
          <div><strong>LAE</strong><small>Luma Application Engine</small></div>
        </Link>
        <p className="rail-label">APPLICATION ENGINE</p>
        <a
          className={`rail-button${activeSection === "deployment" ? " is-active" : ""}`}
          href="#deployment"
          aria-current={activeSection === "deployment" ? "location" : undefined}
          onClick={() => setActiveSection("deployment")}
        >
          <CloudUpload size={18} strokeWidth={1.7} />
          <span>部署</span>
        </a>
        <a
          className={`rail-button${activeSection === "applications" ? " is-active" : ""}`}
          href="#applications"
          aria-current={activeSection === "applications" ? "location" : undefined}
          onClick={() => setActiveSection("applications")}
        >
          <Boxes size={18} strokeWidth={1.7} />
          <span>应用</span>
        </a>
        <a
          className={`rail-button${activeSection === "activity" ? " is-active" : ""}`}
          href="#activity"
          aria-current={activeSection === "activity" ? "location" : undefined}
          onClick={() => setActiveSection("activity")}
        >
          <Orbit size={18} strokeWidth={1.7} />
          <span>活动</span>
        </a>
        <div className="rail-spacer" />
        <a
          className={`rail-button${activeSection === "cli" ? " is-active" : ""}`}
          href="#cli"
          aria-current={activeSection === "cli" ? "location" : undefined}
          onClick={() => setActiveSection("cli")}
        >
          <Command size={18} strokeWidth={1.7} />
          <span>CLI</span>
        </a>
      </aside>

      <div className="console-main">
        <Header
          principal={principal}
          catalogStatus={catalogStatus}
          activeSection={activeSection}
          theme={theme}
          onToggleTheme={toggleTheme}
        />
        <section className="workspace">
          <AnimatePresence mode="wait" initial={false}>
            <motion.div
              className={`workspace-view view-${activeSection}`}
              key={activeSection}
              initial={reduceMotion ? false : { opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              exit={reduceMotion ? { opacity: 1 } : { opacity: 0, y: -4 }}
              transition={{ duration: reduceMotion ? 0 : 0.16, ease: [0.22, 1, 0.36, 1] }}
            >
              <div className="hero-copy">
                <div>
                  <p className="section-kicker">{activeCopy.eyebrow}</p>
                  <h1>{activeCopy.title}</h1>
                  <span>{activeCopy.note}</span>
                </div>
                {activeSection === "deployment" && (
                  <button className="hero-primary" type="button" onClick={resetFlow}>
                    <CloudUpload size={15} /> 新建部署
                  </button>
                )}
                {activeSection === "applications" && (
                  <a className="hero-primary" href="#deployment" onClick={() => setActiveSection("deployment")}>
                    <CloudUpload size={15} /> 新建部署
                  </a>
                )}
                {activeSection === "activity" && (
                  <button className="hero-primary" type="button" onClick={() => setCatalogRefresh((value) => value + 1)}>
                    <RefreshCw size={15} /> 刷新活动
                  </button>
                )}
                {activeSection === "cli" && (
                  <Link className="hero-primary" href="/account">
                    <Command size={15} /> 管理 Token
                  </Link>
                )}
              </div>

              {activeSection === "deployment" && (
                <>
                  <div className="overview-strip" aria-label="工作区概览">
                    <article><span>运行中</span><strong>{shoreApplications.filter((item) => item.tone === "healthy").length}</strong><small>applications</small></article>
                    <article><span>应用总数</span><strong>{catalogLoading ? "—" : shoreApplications.length}</strong><small>tenant catalog</small></article>
                    <article><span>已验证模板</span><strong>{templatesLoading ? "—" : templates.length}</strong><small>agent passed</small></article>
                    <article><span>来源</span><strong>3</strong><small>Git · File · Compose</small></article>
                  </div>

                  <div className="main-grid console-anchor" id="deployment">
                    <TemplateLake
                      templates={templates}
                      selected={selectedTemplate}
                      onSelect={setSelectedTemplate}
                      loading={templatesLoading}
                      notice={templatesNotice}
                      reduced={Boolean(reduceMotion)}
                    />
                    <DeploymentInstrument
                      flow={flow}
                      source={source}
                      template={selectedTemplate}
                      fileInputId={fileInputId}
                      selectedFile={selectedFile}
                      authenticated={principal !== null}
                      configuration={deploymentConfiguration}
                      plan={deploymentPlan}
                      liveDeployment={liveDeployment}
                      environmentSaving={environmentSaving}
                      events={operationEvents}
                      error={flowError}
                      onSource={selectSource}
                      onFile={selectFile}
                      onAnalyzeGit={beginGitDiagnosis}
                      onAnalyzeUpload={beginUploadDiagnosis}
                      onLaunchTemplate={beginTemplateDiagnosis}
                      onSaveEnvironment={saveEnvironment}
                      onDeploy={deploy}
                      onReset={resetFlow}
                      reduced={Boolean(reduceMotion)}
                    />
                  </div>
                </>
              )}

              {activeSection === "applications" && (
                <ApplicationShore
                  applications={shoreApplications}
                  authenticated={principal !== null}
                  loading={catalogLoading}
                  notice={catalogNotice}
                  onRefresh={() => setCatalogRefresh((value) => value + 1)}
                  busyApplicationIds={lifecycleBusy}
                  onAction={runLifecycleAction}
                  onConfirmAction={(application, action) =>
                    setConfirmedLifecycleAction({ application, action })
                  }
                  onInspect={inspectApplication}
                />
              )}

              {(activeSection === "activity" || activeSection === "cli") && (
                <ConsoleUtilities
                  section={activeSection}
                  flow={flow}
                  events={operationEvents}
                  operations={recentOperations}
                  recoveryBusy={recoveryBusy}
                  error={flowError}
                  onRecover={(operation) => void recoverHistoricalOperation(operation)}
                />
              )}
            </motion.div>
          </AnimatePresence>
        </section>
      </div>
      <AnimatePresence>
        {observingApplication && (
          <ApplicationObservatory
            application={observingApplication}
            logs={applicationLogs}
            metrics={applicationMetrics}
            loading={observabilityLoading}
            notice={observabilityNotice}
            reduced={Boolean(reduceMotion)}
            onClose={closeObservability}
            onRefresh={() => void inspectApplication(observingApplication)}
          />
        )}
        {confirmedLifecycleAction && (
          <LifecycleConfirmation
            request={confirmedLifecycleAction}
            busy={lifecycleBusy.has(confirmedLifecycleAction.application.id)}
            reduced={Boolean(reduceMotion)}
            onClose={() => setConfirmedLifecycleAction(null)}
            onConfirm={() => {
              const request = confirmedLifecycleAction;
              const input =
                request.action === "rollback"
                  ? {
                      deploymentId:
                        request.application.rollbackDeploymentId || undefined,
                    }
                  : {};
              setConfirmedLifecycleAction(null);
              void runLifecycleAction(request.application, request.action, input);
            }}
          />
        )}
      </AnimatePresence>
    </main>
  );
}

function Header({
  principal,
  catalogStatus,
  activeSection,
  theme,
  onToggleTheme,
}: {
  principal: LaePrincipal | null;
  catalogStatus: CatalogStatus;
  activeSection: ConsoleSection;
  theme: "light" | "dark";
  onToggleTheme: () => void;
}) {
  const accountName = principal?.user.email.split("@", 1)[0] || "登录";
  const catalogAvailable = catalogStatus === "connected";
  return (
    <header className="topbar">
      <div className="topbar-breadcrumbs">
        <span>LAE</span><ChevronRight size={12} /><strong>{sectionCopy[activeSection].breadcrumb}</strong>
      </div>
      <div className="runtime-status">
        <span className={`runtime-pulse${catalogAvailable ? "" : " is-muted"}`} />
        {catalogStatus === "checking"
          ? "Checking LAE API"
          : catalogAvailable
            ? "LAE API connected"
            : "LAE API unavailable"}
      </div>
      <div className="account">
        <button className="theme-toggle" type="button" onClick={onToggleTheme} aria-label={theme === "dark" ? "切换到浅色主题" : "切换到深色主题"}>
          {theme === "dark" ? <Sun size={15} /> : <Moon size={15} />}
        </button>
        <span className="plan-badge">{principal?.entitlement.plan.toUpperCase() || "GUEST"}</span>
        <Link className="account-button" aria-label={principal ? "账户" : "登录"} href={principal ? "/account" : "/login"}>
          <CircleUserRound size={18} strokeWidth={1.5} />
          <span>{accountName}</span>
          <ChevronRight size={14} strokeWidth={1.5} />
        </Link>
      </div>
    </header>
  );
}

function TemplateLake({
  templates,
  selected,
  onSelect,
  loading,
  notice,
  reduced,
}: {
  templates: Template[];
  selected: Template | null;
  onSelect: (template: Template) => void;
  loading: boolean;
  notice: string | null;
  reduced: boolean;
}) {
  return (
    <section className="lake" aria-labelledby="template-title">
      <div className="lake-heading">
        <div>
          <span className="section-index">01</span>
          <h2 id="template-title">应用模板</h2>
        </div>
        <span className="lake-note">模板同样执行完整诊断</span>
      </div>
      <div className="water-field">
        {!templates.length && (
          <div className="lake-status" role="status">
            {loading ? <RefreshCw className="stage-spin" size={15} /> : <Orbit size={15} />}
            <span>{loading ? "正在读取已验证模板" : notice || "当前没有可用模板"}</span>
          </div>
        )}
        {templates.map((template, index) => {
          const Icon = template.icon;
          const active = selected?.id === template.id;
          return (
            <motion.button
              type="button"
              key={template.id}
              className={`template-float tone-${template.tone}${active ? " is-selected" : ""}`}
              initial={reduced ? false : { opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{
                duration: 0.18,
                delay: Math.min(index * 0.035, 0.18),
              }}
              onClick={() => onSelect(template)}
              aria-pressed={active}
            >
              <span className="template-icon"><Icon size={22} strokeWidth={1.45} /></span>
              <span className="template-label">
                <strong>{template.name}</strong>
                <small>{template.stack}</small>
                <em>{template.description}</em>
              </span>
              <ArrowRight size={14} />
            </motion.button>
          );
        })}
        <AnimatePresence>
          {selected && (
            <motion.div
              className="selected-current"
              initial={reduced ? false : { opacity: 0, y: 10, scale: 0.97 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 6 }}
              transition={{ duration: 0.38, ease: [0.4, 0, 0.2, 1] }}
            >
              <Sparkles size={14} strokeWidth={1.5} />
              <span><strong>{selected.name}</strong> 已选中，继续开始诊断</span>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </section>
  );
}

function DeploymentInstrument({
  flow,
  source,
  template,
  fileInputId,
  selectedFile,
  authenticated,
  configuration,
  plan,
  liveDeployment,
  environmentSaving,
  events,
  error,
  onSource,
  onFile,
  onAnalyzeGit,
  onAnalyzeUpload,
  onLaunchTemplate,
  onSaveEnvironment,
  onDeploy,
  onReset,
  reduced,
}: {
  flow: FlowState;
  source: SourceKind | null;
  template: Template | null;
  fileInputId: string;
  selectedFile: File | null;
  authenticated: boolean;
  configuration: DeploymentConfiguration | null;
  plan: DeploymentConfiguration | null;
  liveDeployment: LiveDeployment | null;
  environmentSaving: boolean;
  events: OperationEvent[];
  error: string | null;
  onSource: (source: SourceKind) => void;
  onFile: (file: File | null) => void;
  onAnalyzeGit: (input: GitSourceInput) => Promise<void>;
  onAnalyzeUpload: (input: { name: string; slug: string; file: File }) => Promise<void>;
  onLaunchTemplate: (template: Template) => Promise<void>;
  onSaveEnvironment: (
    values: Record<string, { value: string }>,
  ) => Promise<void>;
  onDeploy: () => void | Promise<void>;
  onReset: () => void;
  reduced: boolean;
}) {
  const copy = stateCopy[flow];
  const phases = ["来源", "诊断", "配置", "部署", "上线"];
  const phaseIndex =
    flow === "idle"
      ? 0
      : flow === "configuring"
        ? configuration
          ? 2
          : 0
        : flow === "diagnosing"
          ? 1
          : flow === "ready" || flow === "deploying"
            ? 3
            : 4;
  const locked = flow === "diagnosing" || flow === "deploying";

  return (
    <section className="instrument" aria-labelledby="deployment-title" aria-live="polite">
      <div className="instrument-topline">
        <span>{copy.eyebrow}</span>
        <span>{locked ? "IN PROGRESS" : flow === "live" ? "VERIFIED" : "READY FOR INPUT"}</span>
      </div>
      <ol className="phase-track" aria-label={`部署阶段：${phases[phaseIndex]}`}>
        {phases.map((phase, index) => (
          <li
            key={phase}
            className={index < phaseIndex ? "is-complete" : index === phaseIndex ? "is-current" : ""}
            aria-current={index === phaseIndex ? "step" : undefined}
          >
            <span>{index < phaseIndex ? <Check size={10} /> : index + 1}</span>
            <small>{phase}</small>
          </li>
        ))}
      </ol>

      <AnimatePresence mode="wait">
        <motion.div
          key={flow}
          className="instrument-copy"
          initial={reduced ? false : { opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -7 }}
          transition={{ duration: 0.35, ease: [0.4, 0, 0.2, 1] }}
        >
          <h2 id="deployment-title">{copy.title}</h2>
          <p>{copy.note}</p>
        </motion.div>
      </AnimatePresence>

      {flow === "idle" ? (
        <div className="source-list">
          <SourceButton icon={GitFork} title="GitHub 仓库" note="连接账户或粘贴公开地址" onClick={() => onSource("github")} />
          <SourceButton icon={LockKeyhole} title="私有 Git" note="HTTPS token 仅以短期凭据租约使用" onClick={() => onSource("git")} />
          <label className="source-row" htmlFor={fileInputId}>
            <span className="source-icon"><FileArchive size={18} strokeWidth={1.55} /></span>
            <span><strong>静态产物</strong><small>HTML 或打包后的 ZIP</small></span>
            <ArrowRight size={16} strokeWidth={1.5} />
            <input
              id={fileInputId}
              type="file"
              accept=".html,.zip,text/html,application/zip"
              onChange={(event) => onFile(event.currentTarget.files?.[0] || null)}
            />
          </label>
          {template && (
            <button className="template-launch" type="button" onClick={() => void onLaunchTemplate(template)}>
              <Sparkles size={15} />
              一键诊断 {template.name}
              <ArrowRight size={15} />
            </button>
          )}
        </div>
      ) : flow === "configuring" ? (
        configuration ? (
          <EnvironmentConfigurationForm
            configuration={configuration}
            saving={environmentSaving}
            onSubmit={onSaveEnvironment}
          />
        ) : (
          <SourceConfiguration
            source={source}
            selectedFile={selectedFile}
            authenticated={authenticated}
            onAnalyzeGit={onAnalyzeGit}
            onAnalyzeUpload={onAnalyzeUpload}
          />
        )
      ) : flow === "ready" && plan ? (
        <DeploymentPlanReview configuration={plan} />
      ) : flow === "live" && liveDeployment ? (
        <DeploymentHandoff deployment={liveDeployment} />
      ) : (
        <Diagnosis flow={flow} source={source} events={events} />
      )}

      {error && <div className="flow-error" role="alert">{error}</div>}

      <div className="instrument-actions">
        {flow === "ready" && (
          <motion.button className="primary-action" type="button" onClick={onDeploy} initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }}>
            部署到 Luma <ArrowRight size={17} />
          </motion.button>
        )}
        {flow === "live" && (
          <a className="primary-action" href="#applications-title">
            查看应用 <Globe2 size={17} />
          </a>
        )}
        {flow !== "idle" && !locked && (
          <button className="secondary-action" type="button" onClick={onReset}>
            <RotateCcw size={14} /> 重新选择
          </button>
        )}
      </div>

      <div className="trust-note"><LockKeyhole size={12} /> 源码与凭据不会进入 LAE 日志</div>
    </section>
  );
}

function DeploymentPlanReview({
  configuration,
}: {
  configuration: DeploymentConfiguration;
}) {
  const services = configuration.services || [];
  const routes = configuration.routes || [];
  const volumes = configuration.volumes || [];
  const warnings = configuration.warnings || [];
  const totalMemory = services.reduce(
    (sum, service) => sum + service.resources.memoryMiB,
    0,
  );
  return (
    <div className="deployment-plan-review">
      <div className="plan-review-banner">
        <Check size={15} />
        <div>
          <strong>LAE Agent 已生成可部署计划</strong>
          <span>确认服务拓扑与资源后，LAE 才会交给 Luma 执行。</span>
        </div>
      </div>
      <div className="plan-review-grid" aria-label="部署计划摘要">
        <article><span>类型</span><strong>{configuration.kind === "compose" ? "Compose" : "Service"}</strong><small>{configuration.sourceRevisionId.slice(0, 12)}</small></article>
        <article><span>服务</span><strong>{services.length}</strong><small>{services.map((service) => service.key).join(" · ")}</small></article>
        <article><span>公网 HTTP</span><strong>{routes.length}</strong><small>随机 *.itool.tech</small></article>
        <article><span>资源</span><strong>{totalMemory} MiB</strong><small>{volumes.length} persistent volumes</small></article>
      </div>
      <div className="plan-service-table" role="table" aria-label="服务部署明细">
        {services.map((service) => (
          <div className="plan-service-row" role="row" key={service.key}>
            <strong role="cell">{service.key}</strong>
            <span role="cell">{service.role}</span>
            <span role="cell">{service.resources.cpu} CPU</span>
            <small role="cell">{service.resources.memoryMiB} MiB · {service.imageSource}</small>
          </div>
        ))}
      </div>
      <div className="plan-review-notes">
        <span>{configuration.environment.length} environment bindings</span>
        <span>{volumes.length ? `${volumes.length} volumes` : "no persistent volume"}</span>
        <span>HTTP only · TCP relay disabled</span>
        {warnings.map((warning) => <span key={warning}>{warning}</span>)}
      </div>
    </div>
  );
}

function DeploymentHandoff({ deployment }: { deployment: LiveDeployment }) {
  const [copied, setCopied] = useState(false);
  const routes = deployment.application?.routes || [];
  const copyRoutes = async () => {
    if (!routes.length) return;
    try {
      await navigator.clipboard.writeText(
        routes.map((route) => `https://${route.hostname}`).join("\n"),
      );
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };
  return (
    <div className="deployment-handoff">
      <div className="handoff-heading">
        <span><Check size={15} /></span>
        <div>
          <strong>运行态验证通过</strong>
          <small>域名会在后续更新与重启时保持稳定</small>
        </div>
        {routes.length > 0 && (
          <button type="button" onClick={() => void copyRoutes()}>
            <Copy size={13} /> {copied ? "已复制" : "复制全部"}
          </button>
        )}
      </div>
      {routes.length ? (
        <div className="handoff-routes">
          {routes.map((route) => (
            <a
              key={`${route.serviceKey}:${route.hostname}`}
              href={`https://${route.hostname}`}
              target="_blank"
              rel="noopener noreferrer"
            >
              <span>{route.primary ? "PRIMARY" : route.serviceKey.toUpperCase()}</span>
              <strong>{route.hostname}</strong>
              <ExternalLink size={14} />
            </a>
          ))}
        </div>
      ) : (
        <p className="handoff-pending">部署已经完成，应用目录正在同步全部公网 HTTP 路由。</p>
      )}
      <div className="handoff-references">
        <span>deployment <code>{shortReference(deployment.deploymentId)}</code></span>
        <span>operation <code>{shortReference(deployment.operationId)}</code></span>
      </div>
    </div>
  );
}

function SourceButton({ icon: Icon, title, note, onClick }: { icon: LucideIcon; title: string; note: string; onClick: () => void }) {
  return (
    <button className="source-row" type="button" onClick={onClick}>
      <span className="source-icon"><Icon size={18} strokeWidth={1.55} /></span>
      <span><strong>{title}</strong><small>{note}</small></span>
      <ArrowRight size={16} strokeWidth={1.5} />
    </button>
  );
}

function SourceConfiguration({
  source,
  selectedFile,
  authenticated,
  onAnalyzeGit,
  onAnalyzeUpload,
}: {
  source: SourceKind | null;
  selectedFile: File | null;
  authenticated: boolean;
  onAnalyzeGit: (input: GitSourceInput) => Promise<void>;
  onAnalyzeUpload: (input: { name: string; slug: string; file: File }) => Promise<void>;
}) {
  if (!authenticated) {
    return (
      <div className="capability-note">
        <LockKeyhole size={18} />
        <div>
          <strong>先建立安全会话</strong>
          <span>应用、诊断记录和随机域名都必须绑定到你的 personal tenant。</span>
        </div>
        <Link href="/login">注册或登录 <ArrowRight size={14} /></Link>
      </div>
    );
  }
  if (source === "github") {
    return <GitSourceForm onSubmit={onAnalyzeGit} />;
  }
  if (source === "git") {
    return <PrivateGitSourceForm onSubmit={onAnalyzeGit} />;
  }
  if (source === "file") {
    return selectedFile ? (
      <StaticUploadForm file={selectedFile} onSubmit={onAnalyzeUpload} />
    ) : (
      <div className="capability-note">
        <FileArchive size={18} />
        <div><strong>没有选择文件</strong><span>请重新选择 HTML 或 ZIP 静态产物。</span></div>
      </div>
    );
  }
  return null;
}

function EnvironmentConfigurationForm({
  configuration,
  saving,
  onSubmit,
}: {
  configuration: DeploymentConfiguration;
  saving: boolean;
  onSubmit: (
    values: Record<string, { value: string }>,
  ) => Promise<void>;
}) {
  const fieldKey = (item: DeploymentConfiguration["environment"][number]) =>
    `${item.name}:${item.references.join(",")}`;
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      configuration.environment.map((item) => [fieldKey(item), ""]),
    ),
  );
  const [additionalName, setAdditionalName] = useState("");
  const [additionalValue, setAdditionalValue] = useState("");
  const [additionalServiceKey, setAdditionalServiceKey] = useState(
    configuration.serviceKeys[0] || "",
  );
  const [notice, setNotice] = useState<string | null>(null);

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const missing = configuration.environment.filter(
      (item) => item.required && !(values[fieldKey(item)] || "").length,
    );
    if (missing.length) {
      setNotice(`请填写 ${missing.map((item) => item.name).join("、")}。`);
      return;
    }
    const customName = additionalName.trim().toUpperCase();
    if (customName && !/^[A-Z_][A-Z0-9_]{0,127}$/.test(customName)) {
      setNotice("追加变量名只能包含大写字母、数字与下划线。");
      return;
    }
    if (customName && !additionalValue.length) {
      setNotice("请填写追加变量的值。");
      return;
    }
    if (customName && !configuration.serviceKeys.includes(additionalServiceKey)) {
      setNotice("请选择追加变量所属的服务。");
      return;
    }
    const payload: Record<string, { value: string }> = {};
    for (const item of configuration.environment) {
      const value = values[fieldKey(item)] || "";
      if (!value.length && !item.required) continue;
      for (const reference of item.references) payload[reference] = { value };
    }
    if (customName) {
      payload[`${additionalServiceKey}:${customName}`] = { value: additionalValue };
    }
    setNotice(null);
    void onSubmit(payload);
  };

  return (
    <form className="source-form environment-form" onSubmit={submit}>
      <div className="environment-heading">
        <div>
          <strong>补齐运行环境</strong>
          <span>{configuration.kind === "compose" ? `${configuration.serviceKeys.length} 个服务` : "单服务"} · 值只会加密保存</span>
        </div>
        <span>{configuration.environment.filter((item) => item.required).length} required</span>
      </div>
      <div className="environment-fields">
        {configuration.environment.map((item) => (
          <label key={`${item.name}:${item.serviceKeys.join(",")}`}>
            <span className="environment-label">
              <span>{item.name}{item.required ? " *" : ""}</span>
              <small>{item.serviceKeys.join(" · ")}</small>
            </span>
            <input
              required={item.required}
              type={item.sensitive ? "password" : "text"}
              autoComplete="new-password"
              spellCheck={false}
              value={values[fieldKey(item)] || ""}
              onChange={(event) =>
                setValues((current) => ({
                  ...current,
                  [fieldKey(item)]: event.target.value,
                }))
              }
            />
          </label>
        ))}
      </div>
      <details className="connection-creator environment-extra">
        <summary><span>追加一个环境变量</span><ChevronRight size={14} /></summary>
        <div className="source-form-grid">
          <label>
            <span>目标服务</span>
            <select
              value={additionalServiceKey}
              onChange={(event) => setAdditionalServiceKey(event.target.value)}
            >
              {configuration.serviceKeys.map((serviceKey) => (
                <option key={serviceKey} value={serviceKey}>{serviceKey}</option>
              ))}
            </select>
          </label>
          <label>
            <span>变量名</span>
            <input value={additionalName} onChange={(event) => setAdditionalName(event.target.value.toUpperCase())} placeholder="FEATURE_FLAG" />
          </label>
          <label>
            <span>值（默认敏感）</span>
            <input type="password" autoComplete="new-password" spellCheck={false} value={additionalValue} onChange={(event) => setAdditionalValue(event.target.value)} />
          </label>
        </div>
      </details>
      {notice && <div className="connection-notice" role="alert">{notice}</div>}
      <button className="source-submit" type="submit" disabled={saving}>
        {saving ? "正在加密保存…" : "保存并继续"} <ArrowRight size={15} />
      </button>
    </form>
  );
}

function StaticUploadForm({
  file,
  onSubmit,
}: {
  file: File;
  onSubmit: (input: { name: string; slug: string; file: File }) => Promise<void>;
}) {
  const stem = file.name.replace(/\.(?:html|zip)$/i, "");
  const [name, setName] = useState(stem.slice(0, 160));
  const [slug, setSlug] = useState(
    stem
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 80),
  );
  const supported = /\.(?:html|zip)$/i.test(file.name);
  const withinWebLimit = file.size > 0 && file.size <= 64 * 1024 * 1024;

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!supported || !withinWebLimit) return;
    void onSubmit({ name: name.trim(), slug: slug.trim(), file });
  };

  const updateName = (value: string) => {
    setName(value);
    if (!slug) {
      setSlug(
        value
          .toLowerCase()
          .replace(/[^a-z0-9]+/g, "-")
          .replace(/^-+|-+$/g, "")
          .slice(0, 80),
      );
    }
  };

  return (
    <form className="source-form" onSubmit={submit}>
      <div className="upload-selection">
        <FileArchive size={17} />
        <div>
          <strong>{file.name}</strong>
          <span>{formatBytes(file.size)} · 上传后由 Luma Builder 隔离扫描</span>
        </div>
      </div>
      <div className="source-form-grid">
        <label>
          <span>应用名称</span>
          <input required maxLength={160} value={name} onChange={(event) => updateName(event.target.value)} />
        </label>
        <label>
          <span>Slug</span>
          <input required maxLength={80} pattern="[a-z0-9][a-z0-9-]*" value={slug} onChange={(event) => setSlug(event.target.value.toLowerCase())} />
        </label>
      </div>
      {!supported && <div className="connection-notice">Web 只接受 `.html` 或 `.zip`。</div>}
      {!withinWebLimit && (
        <div className="connection-notice">Web 端单文件上限为 64 MiB；更大产物请使用流式 LAE CLI。</div>
      )}
      <button className="source-submit" type="submit" disabled={!supported || !withinWebLimit}>
        上传并诊断 <ArrowRight size={15} />
      </button>
    </form>
  );
}

function GitSourceForm({
  privateConnection = false,
  connections,
  onSubmit,
}: {
  privateConnection?: boolean;
  connections?: SourceConnection[];
  onSubmit: (input: GitSourceInput) => Promise<void>;
}) {
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [repository, setRepository] = useState("");
  const [ref, setRef] = useState("main");
  const [subdirectory, setSubdirectory] = useState("");
  const [connectionId, setConnectionId] = useState("");

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void onSubmit({
      name: name.trim(),
      slug: slug.trim(),
      repository: repository.trim(),
      ref: ref.trim() || "main",
      subdirectory: subdirectory.trim(),
      ...(privateConnection ? { connectionId: connectionId.trim() } : {}),
    });
  };

  const updateName = (value: string) => {
    setName(value);
    if (!slug) {
      setSlug(
        value
          .toLowerCase()
          .trim()
          .replace(/[^a-z0-9]+/g, "-")
          .replace(/^-+|-+$/g, "")
          .slice(0, 80),
      );
    }
  };

  return (
    <form className="source-form" onSubmit={submit}>
      <div className="source-form-grid">
        <label>
          <span>应用名称</span>
          <input required maxLength={160} value={name} onChange={(event) => updateName(event.target.value)} placeholder="Northwind Notes" />
        </label>
        <label>
          <span>Slug</span>
          <input required maxLength={80} pattern="[a-z0-9][a-z0-9-]*" value={slug} onChange={(event) => setSlug(event.target.value.toLowerCase())} placeholder="northwind-notes" />
        </label>
      </div>
      <label>
        <span>HTTPS 仓库地址</span>
        <input required type="url" inputMode="url" value={repository} onChange={(event) => setRepository(event.target.value)} placeholder="https://github.com/org/repository.git" />
      </label>
      <div className="source-form-grid">
        <label>
          <span>Branch / tag</span>
          <input required maxLength={255} value={ref} onChange={(event) => setRef(event.target.value)} />
        </label>
        <label>
          <span>项目子目录</span>
          <input maxLength={512} value={subdirectory} onChange={(event) => setSubdirectory(event.target.value)} placeholder="apps/web（可选）" />
        </label>
      </div>
      <div className="source-form-grid">
        {privateConnection ? (
          <label>
            <span>凭据连接</span>
            <select required value={connectionId} onChange={(event) => setConnectionId(event.target.value)}>
              <option value="">选择已保存连接</option>
              {(connections || []).map((connection) => (
                <option value={connection.id} key={connection.id}>
                  {connection.displayName} · {connection.allowedHost}
                </option>
              ))}
            </select>
          </label>
        ) : (
          <div className="source-form-assurance"><LockKeyhole size={13} /> 公开仓库，不发送凭据</div>
        )}
      </div>
      <button className="source-submit" type="submit" disabled={privateConnection && !connectionId}>
        创建应用并诊断 <ArrowRight size={15} />
      </button>
    </form>
  );
}

function PrivateGitSourceForm({
  onSubmit,
}: {
  onSubmit: (input: GitSourceInput) => Promise<void>;
}) {
  const [connections, setConnections] = useState<SourceConnection[]>([]);
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [provider, setProvider] = useState<SourceConnection["provider"]>("github");
  const [displayName, setDisplayName] = useState("");
  const [baseUrl, setBaseUrl] = useState("https://github.com");
  const [username, setUsername] = useState("");
  const [secret, setSecret] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const result = await listSourceConnections(controller.signal);
        if (!controller.signal.aborted) {
          setConnections(result.connections.filter((connection) => !connection.revokedAt));
        }
      } catch (error) {
        if (!controller.signal.aborted) {
          setNotice(
            error instanceof LaeApiError
              ? error.message
              : "暂时无法读取私有 Git 连接。",
          );
        }
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    })();
    return () => controller.abort();
  }, []);

  const changeProvider = (next: SourceConnection["provider"]) => {
    setProvider(next);
    if (next === "github") setBaseUrl("https://github.com");
    else setBaseUrl("");
  };

  const saveConnection = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (creating) return;
    setCreating(true);
    setNotice(null);
    try {
      const result = await createSourceConnection(
        {
          provider,
          displayName: displayName.trim(),
          baseUrl: baseUrl.trim(),
          ...(username.trim() ? { username: username.trim() } : {}),
          secret,
        },
        newIdempotencyKey("source-connection"),
      );
      setConnections((current) => [
        result.connection,
        ...current.filter((item) => item.id !== result.connection.id),
      ]);
      setDisplayName("");
      setUsername("");
      setSecret("");
      setNotice("连接已加密保存；诊断时只会签发任务绑定的短期凭据租约。");
    } catch (error) {
      setSecret("");
      setNotice(
        error instanceof LaeApiError ? error.message : "连接未能保存，请稍后重试。",
      );
    } finally {
      setCreating(false);
    }
  };

  return (
    <div className="private-source-stack">
      {loading ? (
        <div className="connection-loading"><RefreshCw className="stage-spin" size={14} /> 正在读取安全连接</div>
      ) : (
        <GitSourceForm
          privateConnection
          connections={connections}
          onSubmit={onSubmit}
        />
      )}
      <details className="connection-creator" open={!loading && connections.length === 0}>
        <summary>
          <span>{connections.length ? "添加另一条私有连接" : "先配置私有 Git 凭据"}</span>
          <ChevronRight size={14} />
        </summary>
        <form className="source-form connection-form" onSubmit={saveConnection}>
          <div className="source-form-grid">
            <label>
              <span>Provider</span>
              <select value={provider} onChange={(event) => changeProvider(event.target.value as SourceConnection["provider"])}>
                <option value="github">GitHub</option>
                <option value="gitea">Gitea</option>
                <option value="generic">Generic Git HTTPS</option>
              </select>
            </label>
            <label>
              <span>连接名称</span>
              <input required maxLength={120} value={displayName} onChange={(event) => setDisplayName(event.target.value)} placeholder="Work GitHub" />
            </label>
          </div>
          <label>
            <span>Git 服务根地址</span>
            <input required type="url" value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} placeholder="https://git.example.com" />
          </label>
          <div className="source-form-grid">
            <label>
              <span>用户名（可选）</span>
              <input maxLength={256} autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)} />
            </label>
            <label>
              <span>Personal access token</span>
              <input required type="password" maxLength={4096} autoComplete="new-password" spellCheck={false} value={secret} onChange={(event) => setSecret(event.target.value)} />
            </label>
          </div>
          <button className="source-submit" type="submit" disabled={creating}>
            {creating ? "正在加密保存…" : "保存连接"} <LockKeyhole size={14} />
          </button>
        </form>
      </details>
      {notice && <div className="connection-notice" role="status">{notice}</div>}
    </div>
  );
}

function Diagnosis({
  flow,
  source,
  events,
}: {
  flow: FlowState;
  source: string | null;
  events: OperationEvent[];
}) {
  if (events.length) {
    return (
      <ol className="diagnosis-list event-list">
        {events.slice(-5).map((event) => (
          <li key={event.eventId} className={event.status === "failed" ? "is-failed" : "is-done"}>
            <span className="stage-marker">
              {event.status === "failed" ? "!" : <Check size={12} strokeWidth={2} />}
            </span>
            <span>{event.message}</span>
            <small>{event.phase || event.status}</small>
          </li>
        ))}
      </ol>
    );
  }
  const stages = [
    { label: source === "file" ? "校验归档边界" : "解析不可变提交", at: ["diagnosing", "ready", "deploying", "live"] },
    { label: "识别服务与 HTTP 入口", at: ["ready", "deploying", "live"] },
    { label: "生成并签名部署计划", at: ["ready", "deploying", "live"] },
    { label: "构建镜像与安全扫描", at: ["deploying", "live"] },
    { label: "分配域名并验证健康", at: ["live"] },
  ];
  return (
    <ol className="diagnosis-list">
      {stages.map((stage, index) => {
        const done = stage.at.includes(flow);
        const current = !done && (index === stages.findIndex((item) => !item.at.includes(flow)));
        return (
          <li key={stage.label} className={done ? "is-done" : current ? "is-current" : ""}>
            <span className="stage-marker">{done ? <Check size={12} strokeWidth={2} /> : String(index + 1).padStart(2, "0")}</span>
            <span>{stage.label}</span>
            {current && <RefreshCw className="stage-spin" size={13} />}
          </li>
        );
      })}
    </ol>
  );
}

function abortableDelay(milliseconds: number, signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException("Operation watch canceled", "AbortError"));
      return;
    }
    const timer = window.setTimeout(() => {
      signal.removeEventListener("abort", abort);
      resolve();
    }, milliseconds);
    const abort = () => {
      window.clearTimeout(timer);
      reject(new DOMException("Operation watch canceled", "AbortError"));
    };
    signal.addEventListener("abort", abort, { once: true });
  });
}

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}

function ApplicationShore({
  applications,
  authenticated,
  loading,
  notice,
  onRefresh,
  busyApplicationIds,
  onAction,
  onConfirmAction,
  onInspect,
}: {
  applications: ShoreApplication[];
  authenticated: boolean;
  loading: boolean;
  notice: string | null;
  onRefresh: () => void;
  busyApplicationIds: Set<string>;
  onAction: (application: ShoreApplication, action: ApplicationAction) => void;
  onConfirmAction: (
    application: ShoreApplication,
    action: "rollback" | "delete",
  ) => void;
  onInspect: (application: ShoreApplication) => void;
}) {
  const serviceCount = applications.reduce((total, app) => total + app.services, 0);
  return (
    <section
      className="application-shore console-anchor"
      id="applications"
      aria-labelledby="applications-title"
    >
      <div className="shore-title">
        <span className="section-index">02</span>
        <div>
          <h2 id="applications-title">你的应用</h2>
          <p>{loading ? "正在读取…" : `${applications.length} 个应用 · ${serviceCount} 个服务`}</p>
        </div>
        <button type="button" onClick={onRefresh} disabled={loading}>
          刷新 <RefreshCw size={14} />
        </button>
        {notice && <p className="shore-notice" role="status">{notice}</p>}
      </div>
      <div className="application-ribbon">
        {applications.map((app) => (
          <article className="application-item" key={app.id}>
            <span className={`app-status ${app.tone}`} />
            <div><Link className="application-name-link" href={`/applications/${app.id}`}>{app.name}</Link><small>{app.domain}</small></div>
            <span className="service-count">
              {app.status} · {app.services} service{app.services > 1 ? "s" : ""}
            </span>
            <div className="app-actions">
              <button
                type="button"
                disabled={!app.lifecycleEnabled}
                title="查看实时日志与最近一小时指标"
                aria-label={`${app.name} 运行观测`}
                onClick={() => onInspect(app)}
              >
                <Activity size={14} />
              </button>
              <button
                type="button"
                disabled={!app.lifecycleEnabled || busyApplicationIds.has(app.id)}
                title={app.tone === "paused" ? "恢复应用" : "暂停并保留域名、配置与卷"}
                aria-label={`${app.name} ${app.tone === "paused" ? "恢复" : "暂停"}`}
                onClick={() => onAction(app, app.tone === "paused" ? "resume" : "suspend")}
              >
                {app.tone === "paused" ? <Play size={14} /> : <Pause size={14} />}
              </button>
              <button
                type="button"
                disabled={!app.lifecycleEnabled || app.tone === "paused" || busyApplicationIds.has(app.id)}
                title="重启当前版本"
                aria-label={`${app.name} 重启`}
                onClick={() => onAction(app, "restart")}
              >
                <RotateCcw size={14} />
              </button>
              <button
                type="button"
                disabled={!app.lifecycleEnabled || busyApplicationIds.has(app.id)}
                title="检查源代码是否有可部署更新"
                aria-label={`${app.name} 检查更新`}
                onClick={() => onAction(app, "check-update")}
              >
                <GitFork size={14} />
              </button>
              <button
                type="button"
                disabled={
                  !app.lifecycleEnabled ||
                  !app.rollbackDeploymentId ||
                  busyApplicationIds.has(app.id)
                }
                title={
                  app.rollbackDeploymentId
                    ? "回滚到上一健康版本"
                    : "没有可回滚的健康版本"
                }
                aria-label={`${app.name} 回滚到上一健康版本`}
                onClick={() => onConfirmAction(app, "rollback")}
              >
                <Undo2 size={14} />
              </button>
              <button
                type="button"
                disabled={busyApplicationIds.has(app.id)}
                className="danger-action"
                title="删除应用（默认保留持久卷）"
                aria-label={`${app.name} 删除应用`}
                onClick={() => onConfirmAction(app, "delete")}
              >
                <Trash2 size={14} />
              </button>
            </div>
          </article>
        ))}
        {!loading && applications.length === 0 && (
          <div className="application-empty">
            <div>
              <strong>{authenticated ? "还没有应用" : "先建立一个安全会话"}</strong>
              <span>{notice || "创建应用后，诊断与部署进度会在这里成为真实记录。"}</span>
            </div>
            {authenticated ? (
              <a href="#deployment">开始部署 <ArrowRight size={14} /></a>
            ) : (
              <Link href="/login">注册或登录 <ArrowRight size={14} /></Link>
            )}
          </div>
        )}
      </div>
    </section>
  );
}

function ConsoleUtilities({
  section,
  flow,
  events,
  operations,
  recoveryBusy,
  error,
  onRecover,
}: {
  section: "activity" | "cli";
  flow: FlowState;
  events: OperationEvent[];
  operations: OperationListItem[];
  recoveryBusy: string | null;
  error: string | null;
  onRecover: (operation: OperationListItem) => void;
}) {
  const recentEvents = events.slice(-4).reverse();
  return (
    <div className="console-utilities is-single">
      {section === "activity" && (
        <section className="utility-panel console-anchor" id="activity" aria-labelledby="activity-title">
        <div className="utility-heading">
          <span className="section-index">03</span>
          <div>
            <p>OPERATION STREAM</p>
            <h2 id="activity-title">部署活动</h2>
          </div>
          <span className={`utility-status is-${flow}`}>{stateCopy[flow].eyebrow}</span>
        </div>
        {error && <p className="shore-notice" role="alert">{error}</p>}
        {recentEvents.length ? (
          <ol className="activity-ledger" aria-live="polite">
            {recentEvents.map((event) => (
              <li key={event.eventId} className={event.level === "error" ? "is-error" : ""}>
                <span>{String(event.cursor).padStart(2, "0")}</span>
                <div><strong>{event.message}</strong><small>{event.phase || event.status}</small></div>
              </li>
            ))}
          </ol>
        ) : operations.length ? (
          <ol className="activity-ledger operation-history" aria-label="最近操作">
            {operations.slice(0, 5).map((operation) => (
              <li key={operation.id} className={operation.status === "failed" ? "is-error" : ""}>
                <span>{operation.kind.split(".")[0].slice(0, 2).toUpperCase()}</span>
                <div><strong>{operation.kind}</strong><small>{operation.phase || operation.status} · {shortReference(operation.id)}</small></div>
                <button type="button" onClick={() => onRecover(operation)} disabled={recoveryBusy !== null}>
                  {recoveryBusy === operation.id ? <RefreshCw className="stage-spin" size={12} /> : <Activity size={12} />}
                  {operation.terminal ? "查看" : "恢复"}
                </button>
              </li>
            ))}
          </ol>
        ) : (
          <div className="utility-empty">
            <Activity size={17} strokeWidth={1.45} />
            <div>
              <strong>{error ? "最近一次诊断需要处理" : "还没有部署活动"}</strong>
              <span>{error || "开始一次诊断后，结构化进度会留在这里。"}</span>
            </div>
          </div>
        )}
        </section>
      )}

      {section === "cli" && (
        <section className="utility-panel console-anchor" id="cli" aria-labelledby="cli-title">
          <div className="utility-heading">
            <span className="section-index">04</span>
            <div>
              <p>AGENT-FRIENDLY</p>
              <h2 id="cli-title">LAE CLI</h2>
            </div>
            <SquareTerminal size={18} strokeWidth={1.4} />
          </div>
          <p className="cli-description">Deploy token 授权后，CLI 与控制台走同一套诊断和部署协议，并持续输出机器可读进度。</p>
          <div className="cli-command" aria-label="LAE CLI 示例命令">
            <code><span>$</span> lae login --token-stdin</code>
            <code><span>$</span> lae inspect --app &lt;id&gt; --repo &lt;url&gt; --ref &lt;ref&gt; --idempotency-key &lt;key&gt;</code>
          </div>
          <Link className="utility-link" href="/account">
            管理 Deploy token <ArrowRight size={14} />
          </Link>
        </section>
      )}
    </div>
  );
}

function LifecycleConfirmation({
  request,
  busy,
  reduced,
  onClose,
  onConfirm,
}: {
  request: ConfirmedLifecycleAction;
  busy: boolean;
  reduced: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const rollback = request.action === "rollback";
  const application = request.application;
  const title = rollback ? "回滚到上一版本" : "删除这个应用";

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [busy, onClose]);

  return (
    <motion.div
      className="lifecycle-confirmation-veil"
      initial={reduced ? false : { opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      onMouseDown={(event) => {
        if (!busy && event.currentTarget === event.target) onClose();
      }}
    >
      <motion.section
        className={`lifecycle-confirmation ${rollback ? "rollback" : "delete"}`}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="lifecycle-confirmation-title"
        aria-describedby="lifecycle-confirmation-description"
        initial={reduced ? false : { opacity: 0, y: 18, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        exit={{ opacity: 0, y: 10, scale: 0.99 }}
        transition={{ duration: 0.28, ease: [0.22, 1, 0.36, 1] }}
      >
        <div className="lifecycle-confirmation-mark">
          {rollback ? <Undo2 size={19} /> : <Trash2 size={19} />}
        </div>
        <p>{rollback ? "REVISION RESTORE" : "APPLICATION RETIREMENT"}</p>
        <h2 id="lifecycle-confirmation-title">{title}</h2>
        <div id="lifecycle-confirmation-description">
          <strong>{application.name}</strong>
          {rollback ? (
            <span>
              当前域名和持久卷保持不变；平台会恢复上一健康版本，并在真实
              HTTP 检查通过后切换流量。
            </span>
          ) : (
            <span>
              应用路由与运行实例会被移除。持久卷默认保留，后续清理必须由管理员
              走单独的保留策略。
            </span>
          )}
        </div>
        {rollback && application.rollbackDeploymentId && (
          <code>{shortReference(application.rollbackDeploymentId)}</code>
        )}
        <div className="lifecycle-confirmation-actions">
          <button type="button" onClick={onClose} disabled={busy}>
            保持现状
          </button>
          <button type="button" onClick={onConfirm} disabled={busy} autoFocus>
            {busy ? "正在提交…" : rollback ? "确认回滚" : "确认删除"}
          </button>
        </div>
      </motion.section>
    </motion.div>
  );
}

function ApplicationObservatory({
  application,
  logs,
  metrics,
  loading,
  notice,
  reduced,
  onClose,
  onRefresh,
}: {
  application: ShoreApplication;
  logs: ApplicationLogTail | null;
  metrics: ApplicationMetricHistory | null;
  loading: boolean;
  notice: string | null;
  reduced: boolean;
  onClose: () => void;
  onRefresh: () => void;
}) {
  const series = Object.entries(metrics?.series || {}).slice(0, 6);
  return (
    <motion.div
      className="observatory-veil"
      initial={reduced ? false : { opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      onMouseDown={(event) => {
        if (event.currentTarget === event.target) onClose();
      }}
    >
      <motion.section
        className="observatory"
        role="dialog"
        aria-modal="true"
        aria-labelledby="observatory-title"
        initial={reduced ? false : { opacity: 0, y: 28, scale: 0.985 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        exit={{ opacity: 0, y: 18, scale: 0.99 }}
        transition={{ duration: 0.34, ease: [0.22, 1, 0.36, 1] }}
      >
        <header className="observatory-header">
          <div className="observatory-heading">
            <span className={`app-status ${application.tone}`} />
            <div>
              <p>RUNTIME OBSERVATORY</p>
              <h2 id="observatory-title">{application.name}</h2>
              <small>{logs?.serviceKey || "正在定位主服务"} · {application.domain}</small>
            </div>
          </div>
          <div className="observatory-tools">
            <button type="button" onClick={onRefresh} disabled={loading}>
              <RefreshCw size={14} className={loading ? "stage-spin" : undefined} />
              刷新
            </button>
            <button type="button" onClick={onClose} aria-label="关闭运行观测">
              <X size={16} />
            </button>
          </div>
        </header>

        {notice && <p className="observatory-notice" role="status">{notice}</p>}
        <div className="observatory-grid">
          <section className="metric-chamber" aria-labelledby="metric-title">
            <div className="chamber-title">
              <div><span>01</span><h3 id="metric-title">最近一小时</h3></div>
              <small>{formatObservedAt(metrics?.updatedAt)}</small>
            </div>
            {loading && !metrics ? (
              <div className="observatory-loading"><i /><i /><i /></div>
            ) : series.length ? (
              <div className="metric-series">
                {series.map(([name, points]) => (
                  <article key={name}>
                    <div>
                      <span>{metricName(name)}</span>
                      <strong>{formatMetricValue(name, points.at(-1)?.[1])}</strong>
                    </div>
                    <MetricTrace points={points} />
                  </article>
                ))}
              </div>
            ) : (
              <p className="chamber-empty">还没有可绘制的运行指标。</p>
            )}
          </section>

          <section className="log-chamber" aria-labelledby="log-title">
            <div className="chamber-title">
              <div><span>02</span><h3 id="log-title">服务回声</h3></div>
              <small>{logs ? `${logs.logs.length} / ${logs.tail} lines` : "等待数据"}</small>
            </div>
            {loading && !logs ? (
              <div className="log-loading"><i /><i /><i /><i /></div>
            ) : logs?.logs.length ? (
              <pre tabIndex={0} aria-label={`${application.name} 服务日志`}>
                {logs.logs.join("\n")}
              </pre>
            ) : (
              <p className="chamber-empty">这个服务暂时没有输出日志。</p>
            )}
            {logs?.truncated && <small className="log-truncated">仅显示最新片段</small>}
          </section>
        </div>
      </motion.section>
    </motion.div>
  );
}

function MetricTrace({ points }: { points: Array<[number, number]> }) {
  const values = points.slice(-40);
  if (values.length < 2) return <div className="metric-trace is-empty" />;
  const minimum = Math.min(...values.map((point) => point[1]));
  const maximum = Math.max(...values.map((point) => point[1]));
  const span = maximum - minimum || 1;
  const path = values
    .map((point, index) => {
      const x = (index / (values.length - 1)) * 180;
      const y = 38 - ((point[1] - minimum) / span) * 32;
      return `${index ? "L" : "M"}${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(" ");
  return (
    <svg className="metric-trace" viewBox="0 0 180 44" aria-hidden="true">
      <path className="metric-wash" d={`${path} L180,44 L0,44 Z`} />
      <path className="metric-line" d={path} />
    </svg>
  );
}

function metricName(name: string) {
  return ({
    cpuPercent: "CPU",
    memoryBytes: "MEMORY",
    requestRate: "REQUESTS",
    errorRate: "ERROR RATE",
    p95LatencyMs: "P95 LATENCY",
  } as Record<string, string>)[name] || name.replace(/([a-z])([A-Z])/g, "$1 $2").toUpperCase();
}

function formatMetricValue(name: string, value: number | undefined) {
  if (value === undefined || !Number.isFinite(value)) return "—";
  if (/bytes/i.test(name)) return formatBytes(value);
  if (/requestRate/i.test(name)) return `${value.toFixed(value < 10 ? 2 : 1)} /s`;
  if (/percent|errorRate/i.test(name)) return `${value.toFixed(value < 10 ? 2 : 1)}%`;
  if (/latency|ms/i.test(name)) return `${value.toFixed(0)} ms`;
  return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function formatObservedAt(value: string | undefined) {
  if (!value) return "等待数据";
  const timestamp = new Date(value);
  return Number.isNaN(timestamp.getTime())
    ? "刚刚更新"
    : timestamp.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function shortReference(value: string) {
  return value.length <= 28 ? value : `${value.slice(0, 18)}…${value.slice(-7)}`;
}

function lifecycleActionLabel(action: ApplicationAction) {
  return {
    "check-update": "更新检查",
    suspend: "暂停",
    resume: "恢复",
    restart: "重启",
    rollback: "回滚",
    delete: "删除",
  }[action];
}
