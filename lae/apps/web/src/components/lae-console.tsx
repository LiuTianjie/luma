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
  Trash2,
  Undo2,
  X,
  type LucideIcon,
} from "lucide-react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import Link from "next/link";
import { FormEvent, useEffect, useId, useMemo, useRef, useState } from "react";

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
  listApplications,
  listApplicationDeployments,
  listSourceConnections,
  listTemplates,
  newIdempotencyKey,
  patchApplicationEnvironment,
  requestApplicationAction,
  sha256File,
  staticUploadMediaType,
  transferStaticUpload,
  type LaePrincipal,
  type DeploymentConfiguration,
  type OperationEvent,
  type SourceConnection,
  type ApplicationAction,
  type ApplicationLogTail,
  type ApplicationMetricHistory,
  type ApplicationTemplate,
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

const templatePositions: Record<string, { x: number; y: number; drift: number }> = {
  "nextjs-docker": { x: 18, y: 27, drift: 0.2 },
  "fastapi-minimal": { x: 43, y: 16, drift: 0.8 },
  "flask-hello": { x: 69, y: 32, drift: 1.3 },
  "express-hello": { x: 48, y: 68, drift: 0.5 },
};

function arrangeTemplates(items: ApplicationTemplate[]): Template[] {
  return items.map(({ icon, ...item }, index) => ({
    ...item,
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
  tone: "healthy" | "paused" | "pending";
  desiredState: "running" | "suspended" | "deleted";
  lifecycleEnabled: boolean;
  rollbackDeploymentId: string | null;
};

type ConfirmedLifecycleAction = {
  application: ShoreApplication;
  action: "rollback" | "delete";
};

type GitSourceInput = {
  name: string;
  slug: string;
  repository: string;
  ref: string;
  connectionId?: string;
};

const stateCopy: Record<FlowState, { eyebrow: string; title: string; note: string }> = {
  idle: { eyebrow: "NEW DEPLOYMENT", title: "从哪里开始？", note: "选择源码，LAE Agent 会先判断它是否适合部署。" },
  configuring: { eyebrow: "SOURCE · 01", title: "给应用一个起点", note: "来源信息只用于创建受租户隔离的诊断任务。" },
  diagnosing: { eyebrow: "LAE AGENT · 02", title: "正在读懂这个应用", note: "源码仅在隔离的 Luma Builder 中展开与分析。" },
  ready: { eyebrow: "READY · 03", title: "可以部署", note: "拓扑与端口已确认。部署文件将由 LAE 保存，无需写回仓库。" },
  deploying: { eyebrow: "DEPLOYING · 04", title: "服务正在浮出水面", note: "构建、分配路由并逐个验证 HTTP 服务。" },
  live: { eyebrow: "LIVE · 05", title: "部署完成", note: "应用已进入列表，域名在更新与重启时保持稳定。" },
};

export function LaeConsole() {
  const reduceMotion = useReducedMotion();
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
  const [deploymentConfiguration, setDeploymentConfiguration] =
    useState<DeploymentConfiguration | null>(null);
  const [environmentSaving, setEnvironmentSaving] = useState(false);
  const [activeRun, setActiveRun] = useState<{
    applicationId: string;
    analysisId: string;
    environmentVersion: number;
  } | null>(null);
  const runController = useRef<AbortController | null>(null);
  const observabilityController = useRef<AbortController | null>(null);
  const fileInputId = useId();

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
    setCatalogNotice(null);
    void (async () => {
      try {
        const identity = await getPrincipal(controller.signal);
        const catalog = await listApplications(controller.signal);
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
        setPrincipal(identity);
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
            const running = application.observedState === "running";
            return {
              id: application.id,
              name: application.name,
              domain: primary?.hostname || "等待首次部署",
              status: suspended
                ? "已暂停"
                : running
                  ? "运行中"
                  : application.kind === "pending"
                    ? "待诊断"
                    : "正在准备",
              services: detail?.services.length || 0,
              tone: suspended ? "paused" : running ? "healthy" : "pending",
              desiredState: application.desiredState,
              lifecycleEnabled:
                application.currentDeploymentId !== null && application.kind !== "pending",
              rollbackDeploymentId: rollbackCandidate?.id || null,
            };
          }),
        );
      } catch (error) {
        if (controller.signal.aborted) return;
        setPrincipal(null);
        setShoreApplications([]);
        setCatalogNotice(
          error instanceof LaeApiError && error.status === 401
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
      if (
        operationStatus === "succeeded" &&
        result.status === "needs_configuration" &&
        result.planStored
      ) {
        const configuration = await getDeploymentConfiguration(
          created.application.id,
          analysis.analysis.id,
          controller.signal,
        );
        setDeploymentConfiguration(configuration.configuration);
        setFlow("configuring");
        setCatalogRefresh((value) => value + 1);
        return;
      }
      if (
        operationStatus !== "succeeded" ||
        result.status !== "deployable" ||
        !result.planStored
      ) {
        setFlow("configuring");
        setFlowError(
          "该来源目前不能安全部署，请查看诊断事件后调整代码。",
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
      if (
        operationStatus === "succeeded" &&
        result.status === "needs_configuration" &&
        result.planStored
      ) {
        const configuration = await getDeploymentConfiguration(
          created.application.id,
          analysis.analysis.id,
          controller.signal,
        );
        setDeploymentConfiguration(configuration.configuration);
        setFlow("configuring");
        setCatalogRefresh((value) => value + 1);
        return;
      }
      if (
        operationStatus !== "succeeded" ||
        result.status !== "deployable" ||
        !result.planStored
      ) {
        setFlow("configuring");
        setFlowError(
          "该静态产物目前不能安全部署，请查看诊断事件。",
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
      if (
        operationStatus === "succeeded" &&
        analysis.status === "needs_configuration" &&
        analysis.planStored
      ) {
        const configuration = await getDeploymentConfiguration(
          result.application.id,
          result.analysis.id,
          controller.signal,
        );
        setDeploymentConfiguration(configuration.configuration);
        setFlow("configuring");
        setCatalogRefresh((value) => value + 1);
        return;
      }
      if (
        operationStatus !== "succeeded" ||
        analysis.status !== "deployable" ||
        !analysis.planStored
      ) {
        setFlow("idle");
        setFlowError("该模板未通过当前版本的安全诊断，应用不会进入部署阶段。");
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
    values: Record<string, { value: string; sensitive: boolean; required: boolean }>,
  ) => {
    if (!activeRun || !deploymentConfiguration || environmentSaving) return;
    stopRun();
    const controller = new AbortController();
    runController.current = controller;
    setEnvironmentSaving(true);
    setFlowError(null);
    try {
      const result = await patchApplicationEnvironment(
        activeRun.applicationId,
        {
          expectedVersion: activeRun.environmentVersion,
          set: Object.fromEntries(
            Object.entries(values).map(([name, value]) => [`*:${name}`, value]),
          ),
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
    setEnvironmentSaving(false);
    setFlowError(null);
  };

  return (
    <main className="console-shell">
      <AmbientWater reduced={Boolean(reduceMotion)} />
      <Header
        principal={principal}
        catalogStatus={
          catalogLoading ? "checking" : catalogNotice ? "unavailable" : "connected"
        }
      />

      <aside className="rail" aria-label="主导航">
        <button className="rail-button is-active" aria-label="部署">
          <CloudUpload size={18} strokeWidth={1.7} />
          <span>部署</span>
        </button>
        <button className="rail-button" aria-label="应用">
          <Boxes size={18} strokeWidth={1.7} />
          <span>应用</span>
        </button>
        <button className="rail-button" aria-label="活动">
          <Orbit size={18} strokeWidth={1.7} />
          <span>活动</span>
        </button>
        <div className="rail-spacer" />
        <button className="rail-button" aria-label="命令行">
          <Command size={18} strokeWidth={1.7} />
          <span>CLI</span>
        </button>
      </aside>

      <section className="workspace">
        <div className="hero-copy">
          <motion.div
            initial={reduceMotion ? false : { opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.55, ease: [0.22, 1, 0.36, 1] }}
            className="section-kicker"
          >
            <span className="quiet-dot" /> LUMA APPLICATION ENGINE
          </motion.div>
          <motion.h1
            initial={reduceMotion ? false : { opacity: 0, y: 18 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.7, delay: 0.05, ease: [0.22, 1, 0.36, 1] }}
          >
            让服务落在
            <em>该落的地方。</em>
          </motion.h1>
          <motion.p
            initial={reduceMotion ? false : { opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.55, delay: 0.14, ease: [0.22, 1, 0.36, 1] }}
          >
            带来仓库、静态产物或 Compose。LAE 负责诊断、构建与部署，Luma 负责让它持续运行。
          </motion.p>
        </div>

        <div className="main-grid">
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
      </section>
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
}: {
  principal: LaePrincipal | null;
  catalogStatus: "checking" | "connected" | "unavailable";
}) {
  const accountName = principal?.user.email.split("@", 1)[0] || "登录";
  const catalogAvailable = catalogStatus === "connected";
  return (
    <header className="topbar">
      <div className="brand" aria-label="Luma Application Engine">
        <div className="brand-mark"><span /><span /><span /></div>
        <div>
          <strong>LAE</strong>
          <small>Luma Application Engine</small>
        </div>
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

function AmbientWater({ reduced }: { reduced: boolean }) {
  return (
    <div className="ambient" aria-hidden="true">
      <motion.div
        className="ambient-orb orb-one"
        animate={reduced ? undefined : { x: [0, 34, 0], y: [0, -18, 0] }}
        transition={{ duration: 18, repeat: Infinity, ease: "easeInOut" }}
      />
      <motion.div
        className="ambient-orb orb-two"
        animate={reduced ? undefined : { x: [0, -28, 0], y: [0, 22, 0] }}
        transition={{ duration: 24, repeat: Infinity, ease: "easeInOut" }}
      />
      <div className="grain" />
    </div>
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
          <h2 id="template-title">从一个已知的形状开始</h2>
        </div>
        <span className="lake-note">模板不会绕过诊断</span>
      </div>
      <div className="water-field">
        <div className="water-line line-a" />
        <div className="water-line line-b" />
        <div className="water-line line-c" />
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
              style={{ left: `${template.x}%`, top: `${template.y}%` }}
              initial={reduced ? false : { opacity: 0, scale: 0.82, y: 14 }}
              animate={{
                opacity: 1,
                scale: active ? 1.08 : 1,
                y: reduced ? 0 : [0, index % 2 ? -5 : 5, 0],
              }}
              transition={{
                opacity: { duration: 0.5, delay: Math.min(index * 0.055, 0.32) },
                scale: { duration: 0.35, ease: [0.4, 0, 0.2, 1] },
                y: { duration: 5.5 + template.drift, repeat: Infinity, ease: "easeInOut" },
              }}
              whileHover={reduced ? undefined : { scale: 1.12, y: -7 }}
              whileTap={{ scale: 0.98 }}
              onClick={() => onSelect(template)}
              aria-pressed={active}
            >
              <span className="template-ripple" />
              <span className="template-icon"><Icon size={22} strokeWidth={1.45} /></span>
              <span className="template-label">
                <strong>{template.name}</strong>
                <small>{active ? template.description : template.stack}</small>
              </span>
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
              <span><strong>{selected.name}</strong> 已选中，可在右侧开始诊断</span>
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
  environmentSaving: boolean;
  events: OperationEvent[];
  error: string | null;
  onSource: (source: SourceKind) => void;
  onFile: (file: File | null) => void;
  onAnalyzeGit: (input: GitSourceInput) => Promise<void>;
  onAnalyzeUpload: (input: { name: string; slug: string; file: File }) => Promise<void>;
  onLaunchTemplate: (template: Template) => Promise<void>;
  onSaveEnvironment: (
    values: Record<string, { value: string; sensitive: boolean; required: boolean }>,
  ) => Promise<void>;
  onDeploy: () => void | Promise<void>;
  onReset: () => void;
  reduced: boolean;
}) {
  const copy = stateCopy[flow];
  const progress = useMemo(
    () => ({ idle: 0, configuring: 12, diagnosing: 34, ready: 61, deploying: 83, live: 100 })[flow],
    [flow],
  );
  const locked = flow === "diagnosing" || flow === "deploying";

  return (
    <section className="instrument" aria-labelledby="deployment-title" aria-live="polite">
      <div className="instrument-topline">
        <span>{copy.eyebrow}</span>
        <span>{String(progress).padStart(3, "0")}%</span>
      </div>
      <div className="progress-track"><motion.span animate={{ width: `${progress}%` }} transition={{ duration: reduced ? 0.05 : 0.55, ease: [0.4, 0, 0.2, 1] }} /></div>

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
    values: Record<string, { value: string; sensitive: boolean; required: boolean }>,
  ) => Promise<void>;
}) {
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(configuration.environment.map((item) => [item.name, ""])),
  );
  const [additionalName, setAdditionalName] = useState("");
  const [additionalValue, setAdditionalValue] = useState("");
  const [notice, setNotice] = useState<string | null>(null);

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const missing = configuration.environment.filter(
      (item) => item.required && !(values[item.name] || "").length,
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
    const payload: Record<
      string,
      { value: string; sensitive: boolean; required: boolean }
    > = {};
    for (const item of configuration.environment) {
      const value = values[item.name] || "";
      if (!value.length && !item.required) continue;
      payload[item.name] = {
        value,
        sensitive: item.sensitive,
        required: item.required,
      };
    }
    if (customName) {
      payload[customName] = {
        value: additionalValue,
        sensitive: true,
        required: false,
      };
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
              value={values[item.name] || ""}
              onChange={(event) =>
                setValues((current) => ({ ...current, [item.name]: event.target.value }))
              }
            />
          </label>
        ))}
      </div>
      <details className="connection-creator environment-extra">
        <summary><span>追加一个环境变量</span><ChevronRight size={14} /></summary>
        <div className="source-form-grid">
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
  const [connectionId, setConnectionId] = useState("");

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void onSubmit({
      name: name.trim(),
      slug: slug.trim(),
      repository: repository.trim(),
      ref: ref.trim() || "main",
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
    <section className="application-shore" aria-labelledby="applications-title">
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
            <div><strong>{app.name}</strong><small>{app.domain}</small></div>
            <span className="service-count">{app.services} service{app.services > 1 ? "s" : ""}</span>
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
              <a href="#deployment-title">开始部署 <ArrowRight size={14} /></a>
            ) : (
              <Link href="/login">注册或登录 <ArrowRight size={14} /></Link>
            )}
          </div>
        )}
      </div>
    </section>
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
  const title = rollback ? "回到上一道水位" : "让应用离开岸线";

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
