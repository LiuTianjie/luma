import { ApplicationSecrets } from "./ApplicationSecrets";
import {
  Card,
  CardHeader,
  CardTitle,
  CardDescription,
  CardContent,
  CardAction,
  CardFooter,
} from "@/components/ui/card";
import {
  ApplicationProperties,
  ApplicationVersionEntry,
} from "./ApplicationProperties";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type MouseEvent,
} from "react";
import {
  Box,
  Copy,
  FileText,
  History,
  MoreHorizontal,
  Pencil,
  RotateCw,
  Search,
  Settings2,
  SquareTerminal,
} from "lucide-react";
import {
  fetchDeploymentConfig,
  type DeploymentConfig,
} from "../deploymentConfigApi";
import { apiGet } from "../apiClient";
import { localizeState, t } from "../i18n";
import {
  fetchServiceHistory,
  restartApplication,
  rollbackService,
  updateApplicationStream,
} from "../lifecycleApi";
import { formatTimestamp } from "../format";
import type { DeployStep } from "../deploy/types";
import type {
  DashboardPayload,
  DashboardService,
  Lang,
  ServiceVersion,
} from "../types";
import {
  groupApplications,
  serviceRuntimeStatus,
  type Application,
} from "./applicationModel";
import { applicationEndpoints } from "./applicationEndpoints";
import { ApplicationLogs } from "./ApplicationLogs";
import { useRouter, toHref } from "../router";
import { StepLog } from "../deploy/StepLog";
import { ObservabilityPanel } from "./ObservabilityPanel";
import {
  applicationPath,
  parseApplicationPath,
  APPLICATION_TABS,
} from "./applicationRoutes";
import { useConfirm } from "./ConfirmDialog";
import { Badge, BadgeGroup, CodeCell, StatePill } from "./primitives";
import { Button } from "@/components/ui/button";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import {
  InputGroup,
  InputGroupAddon,
  InputGroupInput,
} from "@/components/ui/input-group";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { AlertCircle } from "lucide-react";

import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Empty,
  EmptyContent,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ScrollArea, ScrollBar } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import {
  Pagination,
  PaginationContent,
  PaginationItem,
} from "@/components/ui/pagination";

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
  errorKind?: "load" | "rollback";
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
  applications,
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
  applications: Application[];
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
  const applicationPage = !selectedStack ? payload?.applicationPage : undefined;
  // Resolve against each fresh snapshot; URL state drives selection and browser back.
  const selected =
    applications.find((app) => app.stack === selectedStack) || null;
  const setSelected = (app: Application | null) =>
    onSelectApplication(app?.stack || null);
  const configRequest = useRef(0);
  const versionsRequest = useRef(0);
  const [detailRefresh, setDetailRefresh] = useState(0);
  useEffect(() => {
    const refresh = () => setDetailRefresh((current) => current + 1);
    window.addEventListener("luma:refresh", refresh);
    return () => window.removeEventListener("luma:refresh", refresh);
  }, []);
  const [deploymentConfig, setDeploymentConfig] =
    useState<DeploymentConfig | null>(null);
  const [deploymentConfigFor, setDeploymentConfigFor] = useState("");
  const [configCopyNotice, setConfigCopyNotice] = useState("");
  const [configTab, setConfigTab] = useState<ConfigTab>("manifest");
  const [actionError, setActionError] = useState("");
  const [actionNotice, setActionNotice] = useState("");
  const [actionBusy, setActionBusy] = useState("");
  const [updatingApp, setUpdatingApp] = useState("");
  const [actionSteps, setActionSteps] = useState<DeployStep[]>([]);
  const [configBusy, setConfigBusy] = useState("");
  const [rollbackState, setRollbackState] = useState<RollbackState | null>(
    null,
  );
  const filters = useMemo<ApplicationFilterState>(() => {
    const params = new URLSearchParams(search);
    return {
      query: params.get("q") || "",
      status: params.get("status") || "all",
      region: params.get("region") || "all",
    };
  }, [search]);
  const setFilters = useCallback(
    (update: (current: ApplicationFilterState) => ApplicationFilterState) => {
      const next = update(filters);
      const params = new URLSearchParams(search);
      for (const [key, value] of [
        ["q", next.query],
        ["status", next.status],
        ["region", next.region],
      ]) {
        if (value && value !== "all") params.set(key, value);
        else params.delete(key);
      }
      params.delete("offset");
      navigate(`${path}${params.size ? `?${params}` : ""}`, { replace: true });
    },
    [filters, search, path, navigate],
  );
  const [searchQuery, setSearchQuery] = useState(filters.query);
  useEffect(() => {
    setSearchQuery(filters.query);
  }, [filters.query]);
  useEffect(() => {
    if (searchQuery === filters.query) return;
    const timer = window.setTimeout(
      () => setFilters((current) => ({ ...current, query: searchQuery })),
      250,
    );
    return () => window.clearTimeout(timer);
  }, [searchQuery, filters.query, setFilters]);
  const changePage = (offset: number) => {
    if (!applicationPage) return;
    const params = new URLSearchParams(search);
    if (offset > 0) params.set("offset", String(offset));
    else params.delete("offset");
    params.set("limit", String(applicationPage.limit));
    navigate(`${path}?${params}`);
  };
  const statusOptions = useMemo(
    () =>
      applicationPage?.statuses ??
      [
        ...new Set(applications.map((app) => app.status).filter(Boolean)),
      ].sort(),
    [applicationPage?.statuses, applications],
  );
  const regionOptions = useMemo(
    () =>
      applicationPage?.regions ??
      [
        ...new Set(applications.flatMap((app) => app.regions).filter(Boolean)),
      ].sort(),
    [applicationPage?.regions, applications],
  );
  const filteredApplications = useMemo(() => {
    if (applicationPage) return applications;
    const query = filters.query.trim().toLowerCase();
    return applications.filter((app) => {
      const matchesStatus =
        filters.status === "all" || app.status === filters.status;
      const matchesRegion =
        filters.region === "all" || app.regions.includes(filters.region);
      const haystack = [
        app.stack,
        ...app.domains,
        ...app.nodes,
        ...app.services.map(
          (service) =>
            `${service.name || ""} ${service.fullName || ""} ${service.image || ""}`,
        ),
      ]
        .join(" ")
        .toLowerCase();
      return (
        matchesStatus && matchesRegion && (!query || haystack.includes(query))
      );
    });
  }, [applications, filters, applicationPage]);

  const restart = async (app: Application) => {
    setActionError("");
    setActionNotice("");
    const ok = await confirm({
      title: lang === "zh" ? `重启 ${app.stack}？` : `Restart ${app.stack}?`,
      body:
        lang === "zh" ? (
          <p>
            当前运行实例会被销毁并重建，重建期间该应用短暂不可用。配置和数据卷不受影响。
          </p>
        ) : (
          <p>
            The running allocation is destroyed and recreated, so the
            application is briefly unavailable. Configuration and volumes are
            untouched.
          </p>
        ),
      confirmLabel: lang === "zh" ? "重启" : "Restart",
      warning:
        lang === "zh"
          ? `影响 ${app.services.length} 个服务 · ${app.running}/${app.desired} 副本`
          : `Affects ${app.services.length} service(s) · ${app.running}/${app.desired} replicas`,
    });
    if (!ok) return;
    setActionBusy(app.stack);
    try {
      const result = await restartApplication({ token, stack: app.stack });
      const replacements = result.replacementAllocations || [];
      if (result.mode !== "recreate" || replacements.length === 0) {
        throw new Error(
          lang === "zh"
            ? "控制面未返回新的运行实例，重启未完成。"
            : "Control did not return a replacement allocation; restart did not complete.",
        );
      }
      const shortIds = replacements.map((id) => id.slice(0, 8)).join(", ");
      setActionNotice(
        lang === "zh"
          ? `应用已重建，新实例：${shortIds}`
          : `Application recreated. New allocation: ${shortIds}`,
      );
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
  const runtimeAppForUpdate = async (
    app: Application,
  ): Promise<Application> => {
    if (!applicationPage) return app;
    // Summary pages deliberately omit volumes and other deployment details.
    // Fetch the complete application before inferring an editable manifest.
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 20000);
    try {
      const detail = await apiGet<DashboardPayload>(
        `/v1/dashboard?scope=application&app=${encodeURIComponent(app.stack)}`,
        token,
        controller.signal,
      );
      const runtimeApp = groupApplications(detail.services || []).find(
        (item) => item.stack === app.stack,
      );
      if (!runtimeApp)
        throw new Error(
          lang === "zh"
            ? `无法读取 ${app.stack} 的完整运行配置，请刷新后重试。`
            : `Could not read the complete runtime configuration for ${app.stack}. Refresh and try again.`,
        );
      return runtimeApp;
    } finally {
      window.clearTimeout(timeout);
    }
  };
  const openUpdate = async (app: Application) => {
    setActionError("");
    setActionSteps([]);
    setConfigBusy(app.stack);
    // Keep this component mounted while configuration, confirmation and the
    // update stream run; list-to-detail navigation changes the data scope.
    let gitUpdateStarted = false;
    let configLoaded = false;
    try {
      const config = await fetchDeploymentConfig({ token, name: app.stack });
      configLoaded = true;
      if (config.gitSource) {
        const source =
          config.gitSource.repository ||
          config.gitSource.repoUrl ||
          config.sourceName ||
          app.stack;
        const ref = config.gitSource.ref;
        const ok = await confirm({
          title:
            lang === "zh"
              ? `从 Git 更新 ${app.stack}？`
              : `Update ${app.stack} from Git?`,
          tone: "neutral",
          body: (
            <>
              <p>
                {lang === "zh"
                  ? "会重新拉取仓库、构建镜像并部署，构建进度在当前应用页面显示，并可在交付记录追溯。"
                  : "Re-clones the repository, builds a new image and deploys it. Progress remains visible on this application page and in delivery history."}
              </p>
              <p>
                <code>
                  {source}
                  {ref ? ` @ ${ref}` : ""}
                </code>
              </p>
            </>
          ),
          confirmLabel: lang === "zh" ? "开始更新" : "Start update",
        });
        if (!ok) return;
        setConfigBusy("");
        gitUpdateStarted = true;
        setUpdatingApp(app.stack);
        let failed = "";
        await updateApplicationStream({ token, name: app.stack }, (step) => {
          setActionSteps((current) => [...current, step]);
          if (step.status === "fail")
            failed = step.message || step.name || "Update failed";
        });
        if (failed) throw new Error(failed);
        setActionNotice(
          lang === "zh"
            ? `${app.stack} 更新流程已完成`
            : `${app.stack} update completed`,
        );
        await onRefresh();
        return;
      }
      if (!onUpdateApplication) {
        setActionError(
          lang === "zh"
            ? "当前页面未配置更新应用入口。"
            : "This page does not have an update-application entry configured.",
        );
        return;
      }
      const updateApp =
        config.manifest || config.composeContent
          ? app
          : await runtimeAppForUpdate(app);
      onUpdateApplication({ app: updateApp, deploymentConfig: config });
    } catch (error) {
      const message = String(error instanceof Error ? error.message : error);
      if (gitUpdateStarted || configLoaded || !onUpdateApplication) {
        setActionError(message);
      } else {
        try {
          const updateApp = await runtimeAppForUpdate(app);
          onUpdateApplication({
            app: updateApp,
            configWarning:
              lang === "zh"
                ? `未读取到已登记部署配置，已从当前运行状态反推；提交前请重点核对 YAML。${message ? ` (${message})` : ""}`
                : `Could not load a registered deployment config, so the form was inferred from current runtime state. Review the YAML carefully before submitting.${message ? ` (${message})` : ""}`,
          });
        } catch (runtimeError) {
          setActionError(
            String(
              runtimeError instanceof Error
                ? runtimeError.message
                : runtimeError,
            ),
          );
        }
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

  const firstLogService = (app: Application) =>
    app.services.find((service) => service.fullName);

  const openApplicationLogs = (app: Application) => {
    const service = firstLogService(app);
    if (!service?.fullName) {
      setActionError(
        lang === "zh"
          ? `应用 ${app.stack} 暂无可读取日志的服务。`
          : `Application ${app.stack} has no service logs available.`,
      );
      return;
    }
    setActionError("");
    navigate(
      applicationPath(app.stack, "logs") +
        `?service=${encodeURIComponent(service.fullName)}`,
    );
  };

  const openServiceLogs = (
    service: DashboardService,
    appServices: DashboardService[],
  ) => {
    if (!service.fullName) {
      setActionError(
        lang === "zh"
          ? "该服务暂无可读取日志。"
          : "This service has no logs available.",
      );
      return;
    }
    setActionError("");
    const stack = service.stack || appServices[0]?.stack || selectedStack;
    if (stack)
      navigate(
        applicationPath(stack, "logs") +
          `?service=${encodeURIComponent(service.fullName)}`,
      );
  };

  const loadVersions = async (app: Application, message = "") => {
    const request = ++versionsRequest.current;
    setActionError("");
    setRollbackState({
      app: app.stack,
      versions: [],
      loading: true,
      error: "",
      message,
      busyVersion: null,
    });
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
        errorKind: "load",
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
      title:
        lang === "zh"
          ? `将 ${app.stack} 回滚到 v${version}？`
          : `Roll ${app.stack} back to v${version}?`,
      body:
        lang === "zh" ? (
          <p>
            运行态会切换回 v{version}{" "}
            的镜像并重建实例。这本身也是一次新部署，之后仍可回滚到当前版本。
          </p>
        ) : (
          <p>
            The runtime switches back to the v{version} image and its allocation
            is recreated. This is itself a new deployment, so you can roll
            forward again afterwards.
          </p>
        ),
      confirmLabel:
        lang === "zh" ? `回滚到 v${version}` : `Roll back to v${version}`,
    });
    if (!ok) return;
    setActionError("");
    setRollbackState((current) =>
      current && current.app === app.stack
        ? { ...current, error: "", message: "", busyVersion: version }
        : current,
    );
    try {
      const result = await rollbackService({ token, name: app.stack, version });
      await onRefresh();
      await loadVersions(
        app,
        result.message ||
          (lang === "zh"
            ? `已回滚到 v${version}`
            : `Rolled back to v${version}`),
      );
    } catch (error) {
      setRollbackState((current) =>
        current && current.app === app.stack
          ? {
              ...current,
              loading: false,
              errorKind: "rollback",
              error: String(error instanceof Error ? error.message : error),
              message: "",
              busyVersion: null,
            }
          : current,
      );
    }
  };

  const selectedDiagnostics =
    selected?.services.flatMap((service) => service.diagnostics || []) || [];
  const selectedVolumes =
    selected?.services.flatMap((service) => service.storage || []) || [];
  const selectedConfig =
    selected && deploymentConfigFor === selected.stack
      ? deploymentConfig
      : null;
  const selectedRollback =
    selected && rollbackState?.app === selected.stack ? rollbackState : null;
  const selectedConfigTabs: ConfigTab[] = [
    ...(selectedConfig?.manifest ? ["manifest" as const] : []),
    ...(selectedConfig?.composeContent ? ["compose" as const] : []),
  ];
  const selectedConfigContent =
    configTab === "compose"
      ? selectedConfig?.composeContent
      : selectedConfig?.manifest;
  const serviceCountLabel = (count: number) =>
    lang === "zh"
      ? `${count} 个服务`
      : `${count} service${count === 1 ? "" : "s"}`;
  const replicaLabel = (running: number, desired: number) =>
    lang === "zh"
      ? `${running}/${desired} 副本`
      : `${running}/${desired} replicas`;
  const logLabel = lang === "zh" ? "日志" : "Logs";
  const shellLabel = t(lang, "shell");
  const serviceIsRunning = (service: DashboardService) => {
    const status = serviceRuntimeStatus(service);
    return (
      (service.running || 0) > 0 || ["running", "healthy"].includes(status)
    );
  };
  useEffect(() => {
    setConfigBusy("");
    setConfigCopyNotice("");
    if (!selected) return;
    setActionError("");
    if (tab === "config") void openConfig(selected);
    if (tab === "versions") void loadVersions(selected);
    return () => {
      configRequest.current += 1;
      versionsRequest.current += 1;
    };
    // Route changes and manual refresh, not polling snapshots, trigger requests.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected?.stack, tab, token, detailRefresh]);
  const activeServices = route.service
    ? selected?.services.filter(
        (service) => (service.fullName || service.name) === route.service,
      ) || []
    : selected?.services || [];
  const followLink = (event: MouseEvent<HTMLElement>, destination: string) => {
    if (
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    )
      return;
    event.preventDefault();
    navigate(destination);
  };
  const moreLabel = lang === "zh" ? "更多操作" : "More actions";
  const renderActions = (app: Application) => (
    <div className="flex flex-wrap items-center gap-2">
      <Button
        variant="outline"
        size="sm"
        disabled={!firstLogService(app)}
        onClick={() => openApplicationLogs(app)}
      >
        <FileText data-icon="inline-start" />
        {logLabel}
      </Button>
      <Button
        variant="outline"
        size="sm"
        disabled={Boolean(configBusy || updatingApp)}
        onClick={() => void openUpdate(app)}
      >
        {updatingApp === app.stack || configBusy === app.stack ? (
          <Spinner aria-hidden="true" data-icon="inline-start" />
        ) : (
          <Pencil data-icon="inline-start" />
        )}
        {updatingApp === app.stack
          ? lang === "zh"
            ? "更新中…"
            : "Updating…"
          : configBusy === app.stack
            ? t(lang, "loadingConfig")
            : t(lang, "updateApp")}
      </Button>
      <DropdownMenu>
        <DropdownMenuTrigger
          render={
            <Button
              variant="outline"
              size="icon"
              aria-label={`${app.stack} · ${moreLabel}`}
            />
          }
        >
          <MoreHorizontal data-icon="inline-start" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuGroup>
            <DropdownMenuItem onClick={() => openDetails(app)}>
              <Settings2 />
              {t(lang, "details")}
            </DropdownMenuItem>
            <DropdownMenuItem
              disabled={
                rollbackState?.app === app.stack && rollbackState.loading
              }
              onClick={() => void openVersions(app)}
            >
              <History />
              {t(lang, "versions")}
            </DropdownMenuItem>
            <DropdownMenuItem
              disabled={Boolean(actionBusy)}
              onClick={() => void restart(app)}
            >
              <RotateCw />
              {actionBusy === app.stack
                ? t(lang, "restarting")
                : t(lang, "restart")}
            </DropdownMenuItem>
          </DropdownMenuGroup>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
  const renderServiceActions = (service: DashboardService) => (
    <div className="flex flex-wrap items-center gap-2">
      <Button
        variant="outline"
        size="sm"
        disabled={!service.fullName}
        onClick={() => openServiceLogs(service, selected?.services || [])}
      >
        <FileText data-icon="inline-start" />
        {logLabel}
      </Button>
      <Button
        variant="outline"
        size="sm"
        disabled={
          !service.fullName || !serviceIsRunning(service) || !onServiceTerminal
        }
        title={
          !serviceIsRunning(service)
            ? lang === "zh"
              ? "服务未运行，无法进入容器"
              : "Service is not running"
            : shellLabel
        }
        onClick={() => {
          if (selected) onServiceTerminal?.(service, selected.stack);
        }}
      >
        <SquareTerminal data-icon="inline-start" />
        {shellLabel}
      </Button>
    </div>
  );
  const appLink = (app: Application) => (
    <Button
      variant="link"
      role="link"
      nativeButton={false}
      render={<a href={toHref(applicationPath(app.stack))} />}
      className="h-auto max-w-full justify-start px-0 whitespace-normal wrap-anywhere"
      onClick={(event) => followLink(event, applicationPath(app.stack))}
    >
      {app.stack}
    </Button>
  );
  const loadingContent = (label: string) => (
    <div className="flex flex-col gap-3" role="status" aria-label={label}>
      <Skeleton className="h-5 w-40" />
      <Skeleton className="h-32 w-full" />
      <span className="sr-only">{label}</span>
    </div>
  );
  const detailPage = selected ? (
    <section
      className="flex min-w-0 flex-col gap-6"
      aria-labelledby="application-detail-title"
    >
      <header className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex min-w-0 flex-col gap-1">
          <h1
            id="application-detail-title"
            className="font-heading text-xl font-semibold tracking-tight wrap-anywhere"
          >
            {selected.stack}
          </h1>
          <p className="text-sm text-muted-foreground">
            {serviceCountLabel(selected.services.length)} ·{" "}
            {replicaLabel(selected.running, selected.desired)}
          </p>
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={Boolean(actionBusy)}
            onClick={() => void restart(selected)}
          >
            {actionBusy === selected.stack ? (
              <Spinner aria-hidden="true" data-icon="inline-start" />
            ) : (
              <RotateCw data-icon="inline-start" />
            )}
            {actionBusy === selected.stack
              ? t(lang, "restarting")
              : t(lang, "restart")}
          </Button>
          <Button
            size="sm"
            disabled={Boolean(configBusy || updatingApp)}
            onClick={() => void openUpdate(selected)}
          >
            {updatingApp === selected.stack || configBusy === selected.stack ? (
              <Spinner aria-hidden="true" data-icon="inline-start" />
            ) : (
              <Pencil data-icon="inline-start" />
            )}
            {updatingApp === selected.stack
              ? lang === "zh"
                ? "更新中…"
                : "Updating…"
              : configBusy === selected.stack
                ? t(lang, "loadingConfig")
                : t(lang, "updateApp")}
          </Button>
        </div>
      </header>
      <Tabs
        value={tab}
        className="min-w-0 gap-6"
        onValueChange={(value, details) => {
          const event = details.event;
          if (
            event instanceof window.MouseEvent &&
            (event.button !== 0 ||
              event.metaKey ||
              event.ctrlKey ||
              event.shiftKey ||
              event.altKey)
          ) {
            details.cancel();
            return;
          }
          const next = APPLICATION_TABS.find((item) => item.id === value);
          if (next) navigate(applicationPath(selected.stack, next.id));
        }}
      >
        <div className="max-w-full overflow-x-auto pb-1">
          <TabsList
            aria-label={lang === "zh" ? "应用工作区" : "Application workspace"}
          >
            {APPLICATION_TABS.map((item) => (
              <TabsTrigger
                key={item.id}
                value={item.id}
                nativeButton={false}
                render={
                  <a
                    href={toHref(applicationPath(selected.stack, item.id))}
                    aria-current={tab === item.id ? "page" : undefined}
                  />
                }
                onClick={(event) => {
                  if (
                    event.button === 0 &&
                    !event.metaKey &&
                    !event.ctrlKey &&
                    !event.shiftKey &&
                    !event.altKey
                  )
                    event.preventDefault();
                }}
              >
                {lang === "zh" ? item.zh : item.en}
              </TabsTrigger>
            ))}
          </TabsList>
        </div>
        <TabsContent value={tab} className="flex min-w-0 flex-col gap-6">
          {tab === "secrets" ? (
            <ApplicationSecrets
              key={selected.stack}
              app={selected.stack}
              token={token}
              lang={lang}
              onUpdate={() => void openUpdate(selected)}
            />
          ) : null}
          {tab === "overview" ? (
            <>
              <Card>
                <CardHeader>
                  <CardTitle>
                    {lang === "zh" ? "运行状态" : "Runtime"}
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  <ApplicationProperties
                    items={[
                      {
                        label: t(lang, "status"),
                        value: (
                          <StatePill
                            value={selected.status}
                            label={localizeState(lang, selected.status)}
                          />
                        ),
                      },
                      {
                        label: t(lang, "replicas"),
                        value: `${selected.running}/${selected.desired}`,
                      },
                      {
                        label: t(lang, "region"),
                        value: selected.regions.join(", ") || "-",
                      },
                      {
                        label: t(lang, "nodes"),
                        value: selected.nodes.join(", ") || "-",
                      },
                      { label: t(lang, "exposure"), value: selected.exposure },
                    ]}
                  />
                </CardContent>
              </Card>
              <Card>
                <CardHeader>
                  <CardTitle>{t(lang, "accessAddress")}</CardTitle>
                </CardHeader>
                <CardContent>
                  {applicationEndpoints(selected.services).length ? (
                    <div className="flex flex-wrap items-start gap-3">
                      {applicationEndpoints(selected.services).map(
                        (endpoint) =>
                          endpoint.href ? (
                            <Button
                              key={endpoint.address}
                              variant="link"
                              role="link"
                              nativeButton={false}
                              className="h-auto max-w-full justify-start px-0 whitespace-normal wrap-anywhere"
                              render={
                                <a
                                  href={endpoint.href}
                                  target="_blank"
                                  rel="noreferrer"
                                />
                              }
                            >
                              {endpoint.address}
                            </Button>
                          ) : (
                            <div
                              className="flex min-w-0 flex-wrap items-center gap-2"
                              key={endpoint.address}
                            >
                              <Badge value="TCP" />
                              <CodeCell value={endpoint.address} />
                            </div>
                          ),
                      )}
                    </div>
                  ) : (
                    <Empty>
                      <EmptyHeader>
                        <EmptyTitle>{t(lang, "internalOnly")}</EmptyTitle>
                        <EmptyDescription>
                          {lang === "zh"
                            ? "当前应用没有对外访问地址。"
                            : "This application has no public endpoint."}
                        </EmptyDescription>
                      </EmptyHeader>
                    </Empty>
                  )}
                </CardContent>
              </Card>
              <Card>
                <CardHeader>
                  <CardTitle>
                    {lang === "zh" ? "存储与诊断" : "Storage and diagnostics"}
                  </CardTitle>
                </CardHeader>
                <CardContent className="flex min-w-0 flex-col gap-4">
                  {selectedVolumes.length ? (
                    <Table
                      containerProps={{
                        tabIndex: 0,
                        role: "region",
                        "aria-label":
                          lang === "zh"
                            ? "应用存储卷，可横向滚动"
                            : "Application volumes, horizontally scrollable",
                      }}
                    >
                      <TableHeader>
                        <TableRow>
                          <TableHead>
                            {lang === "zh" ? "卷 / 路径" : "Volume / path"}
                          </TableHead>
                          <TableHead>
                            {lang === "zh" ? "类型" : "Type"}
                          </TableHead>
                          <TableHead>
                            {lang === "zh"
                              ? "存储类 / 节点"
                              : "Storage class / node"}
                          </TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {selectedVolumes.map((volume, index) => (
                          <TableRow
                            key={`${volume.name}-${volume.storageClass}-${volume.node}-${index}`}
                          >
                            <TableCell className="whitespace-normal wrap-anywhere">
                              <code>{volume.name}</code>
                            </TableCell>
                            <TableCell>{volume.kind || "unmanaged"}</TableCell>
                            <TableCell>
                              {volume.storageClass || volume.node || "-"}
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  ) : (
                    <Empty>
                      <EmptyHeader>
                        <EmptyTitle>
                          {lang === "zh"
                            ? "未发现应用卷"
                            : "No application volumes found"}
                        </EmptyTitle>
                      </EmptyHeader>
                    </Empty>
                  )}
                  <Separator />
                  {selectedDiagnostics.length ? (
                    selectedDiagnostics.map((item, index) => (
                      <Alert key={`${item}-${index}`}>
                        <AlertCircle />
                        <AlertTitle>
                          {lang === "zh" ? "诊断" : "Diagnostics"}
                        </AlertTitle>
                        <AlertDescription>{item}</AlertDescription>
                      </Alert>
                    ))
                  ) : (
                    <p className="text-sm text-muted-foreground">
                      {lang === "zh"
                        ? "暂无诊断告警"
                        : "No diagnostic warnings"}
                    </p>
                  )}
                </CardContent>
              </Card>
            </>
          ) : null}
          {tab === "versions" ? (
            <Card>
              <CardHeader>
                <CardTitle>
                  {lang === "zh" ? "版本历史" : "Version history"}
                </CardTitle>
                <CardDescription>
                  {lang === "zh"
                    ? "查看已部署版本，或回滚到历史版本。"
                    : "Review deployed versions or roll back to an earlier version."}
                </CardDescription>
                <CardAction>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={
                      !selectedRollback ||
                      selectedRollback.loading ||
                      selectedRollback.busyVersion !== null
                    }
                    onClick={() => void loadVersions(selected)}
                  >
                    {selectedRollback?.loading ? (
                      <Spinner aria-hidden="true" data-icon="inline-start" />
                    ) : null}
                    {t(lang, "refresh")}
                  </Button>
                </CardAction>
              </CardHeader>
              <CardContent className="flex min-w-0 flex-col gap-4">
                {selectedRollback?.message ? (
                  <Alert>
                    <AlertDescription>
                      {selectedRollback.message}
                    </AlertDescription>
                  </Alert>
                ) : null}
                {selectedRollback?.error ? (
                  <Alert variant="destructive">
                    <AlertCircle />
                    <AlertTitle>
                      {selectedRollback.errorKind === "rollback"
                        ? lang === "zh"
                          ? "回滚失败"
                          : "Rollback failed"
                        : lang === "zh"
                          ? "无法加载版本历史"
                          : "Could not load version history"}
                    </AlertTitle>
                    <AlertDescription>
                      {selectedRollback.error}
                    </AlertDescription>
                  </Alert>
                ) : null}
                {!selectedRollback || selectedRollback.loading ? (
                  loadingContent(t(lang, "loadingHistory"))
                ) : selectedRollback.versions.length ? (
                  <div className="flex flex-col gap-4">
                    {selectedRollback.versions.map((version, index) => {
                      const targetVersion = versionNumber(version.version);
                      const isCurrent = index === 0;
                      const isBusy =
                        targetVersion !== null &&
                        selectedRollback.busyVersion === targetVersion;
                      return (
                        <ApplicationVersionEntry
                          key={`${version.version ?? "unknown"}-${index}`}
                          version={`v${version.version ?? "-"}`}
                          current={isCurrent}
                          image={version.image || "-"}
                          imageLabel={t(lang, "image")}
                          submitted={versionSubmittedLabel(version.submitTime)}
                          submittedLabel={t(lang, "submitted")}
                          stable={
                            version.stable ? (
                              <Badge value={t(lang, "stableVersion")} />
                            ) : undefined
                          }
                          action={
                            isCurrent ? (
                              <Badge value={t(lang, "currentVersion")} />
                            ) : targetVersion === null ? (
                              <Badge value="-" />
                            ) : (
                              <Button
                                variant="outline"
                                size="sm"
                                disabled={selectedRollback.busyVersion !== null}
                                onClick={() =>
                                  void rollbackToVersion(
                                    selected,
                                    targetVersion,
                                  )
                                }
                              >
                                {isBusy ? (
                                  <Spinner aria-hidden="true" data-icon="inline-start" />
                                ) : null}
                                {isBusy
                                  ? t(lang, "rollingBack")
                                  : t(lang, "rollbackToVersion")}
                              </Button>
                            )
                          }
                        />
                      );
                    })}
                  </div>
                ) : !selectedRollback.error ? (
                  <Empty>
                    <EmptyHeader>
                      <EmptyMedia variant="icon">
                        <History />
                      </EmptyMedia>
                      <EmptyTitle>{t(lang, "noVersionHistory")}</EmptyTitle>
                    </EmptyHeader>
                  </Empty>
                ) : null}
              </CardContent>
            </Card>
          ) : null}
          {tab === "config" && !selectedConfig ? (
            <Card>
              <CardHeader>
                <CardTitle>{t(lang, "deploymentConfig")}</CardTitle>
              </CardHeader>
              <CardContent>
                {configBusy ? (
                  loadingContent(t(lang, "loadingConfig"))
                ) : (
                  <Empty>
                    <EmptyHeader>
                      <EmptyMedia variant="icon">
                        <FileText />
                      </EmptyMedia>
                      <EmptyTitle>{t(lang, "noDeploymentConfig")}</EmptyTitle>
                    </EmptyHeader>
                    <EmptyContent>
                      <Button
                        variant="outline"
                        onClick={() => void openConfig(selected)}
                      >
                        {t(lang, "refresh")}
                      </Button>
                    </EmptyContent>
                  </Empty>
                )}
              </CardContent>
            </Card>
          ) : null}
          {tab === "config" && selectedConfig ? (
            <Card>
              <CardHeader>
                <CardTitle>{t(lang, "deploymentConfig")}</CardTitle>
                <CardDescription className="flex min-w-0 flex-wrap gap-x-4 gap-y-1 wrap-anywhere">
                  <span>
                    {t(lang, "source")}:{" "}
                    <code>{selectedConfig.sourceName || "-"}</code>
                  </span>
                  <span>
                    {t(lang, "lastUpdated")}:{" "}
                    {formatTimestamp(selectedConfig.updatedAt)}
                  </span>
                </CardDescription>
                <CardAction>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={!selectedConfigContent}
                    onClick={() => {
                      setConfigCopyNotice("");
                      void Promise.resolve()
                        .then(() =>
                          navigator.clipboard.writeText(
                            selectedConfigContent || "",
                          ),
                        )
                        .then(() =>
                          setConfigCopyNotice(
                            lang === "zh"
                              ? "已复制完整配置"
                              : "Full configuration copied",
                          ),
                        )
                        .catch(() =>
                          setConfigCopyNotice(
                            lang === "zh"
                              ? "复制失败，请在配置区域选择并复制"
                              : "Copy failed; select and copy the configuration below",
                          ),
                        );
                    }}
                  >
                    <Copy data-icon="inline-start" />
                    {lang === "zh" ? "复制配置" : "Copy configuration"}
                  </Button>
                </CardAction>
              </CardHeader>
              <CardContent className="flex min-w-0 flex-col gap-4">
                <Tabs
                  value={configTab}
                  className="min-w-0 gap-4"
                  onValueChange={(value) => {
                    setConfigTab(value as ConfigTab);
                    setConfigCopyNotice("");
                  }}
                >
                  {selectedConfigTabs.length > 1 ? (
                    <div className="max-w-full overflow-x-auto">
                      <TabsList
                        aria-label={
                          lang === "zh" ? "配置文件" : "Configuration file"
                        }
                      >
                        {selectedConfigTabs.map((item) => (
                          <TabsTrigger key={item} value={item}>
                            {item === "compose"
                              ? t(lang, "composeFile")
                              : t(lang, "lumaManifest")}
                          </TabsTrigger>
                        ))}
                      </TabsList>
                    </div>
                  ) : null}
                  <TabsContent value={configTab} className="min-w-0">
                    {selectedConfigContent ? (
                      <ScrollArea className="h-80 max-h-[60vh]">
                        <pre
                          tabIndex={0}
                          aria-label={t(lang, "deploymentConfig")}
                          className="m-0 w-max min-w-full p-1"
                        >
                          <code>{selectedConfigContent}</code>
                        </pre>
                        <ScrollBar orientation="horizontal" />
                      </ScrollArea>
                    ) : (
                      <Empty>
                        <EmptyHeader>
                          <EmptyTitle>
                            {t(lang, "noDeploymentConfig")}
                          </EmptyTitle>
                        </EmptyHeader>
                      </Empty>
                    )}
                  </TabsContent>
                </Tabs>
                {configCopyNotice ? (
                  <Alert role="status">
                    <AlertDescription>{configCopyNotice}</AlertDescription>
                  </Alert>
                ) : null}
              </CardContent>
            </Card>
          ) : null}
          {tab === "services" ? (
            <>
              {route.service && !activeServices.length ? (
                <Empty>
                  <EmptyHeader>
                    <EmptyMedia variant="icon">
                      <Box />
                    </EmptyMedia>
                    <EmptyTitle>
                      {lang === "zh"
                        ? "服务不存在或已移除"
                        : "Service not found or removed"}
                    </EmptyTitle>
                  </EmptyHeader>
                  <EmptyContent>
                    <Button
                      variant="outline"
                      onClick={() =>
                        navigate(applicationPath(selected.stack, "services"))
                      }
                    >
                      {lang === "zh" ? "返回服务列表" : "Back to services"}
                    </Button>
                  </EmptyContent>
                </Empty>
              ) : null}
              {!route.service ? (
                <Card>
                  <CardHeader>
                    <CardTitle>{t(lang, "services")}</CardTitle>
                    <CardDescription>
                      {serviceCountLabel(selected.services.length)}
                    </CardDescription>
                  </CardHeader>
                  <CardContent>
                    <Table
                      className="min-w-2xl"
                      containerProps={{
                        tabIndex: 0,
                        role: "region",
                        "aria-label":
                          lang === "zh"
                            ? "应用服务，可横向滚动"
                            : "Application services, horizontally scrollable",
                      }}
                    >
                      <TableHeader>
                        <TableRow>
                          <TableHead>{t(lang, "services")}</TableHead>
                          <TableHead>{t(lang, "status")}</TableHead>
                          <TableHead>{t(lang, "replicas")}</TableHead>
                          <TableHead>{t(lang, "nodes")}</TableHead>
                          <TableHead>{t(lang, "actions")}</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {selected.services.map((service) => (
                          <TableRow key={service.fullName || service.name}>
                            <TableCell className="max-w-80 whitespace-normal">
                              <div className="flex min-w-0 flex-col gap-1">
                                <Button
                                  variant="link"
                                  role="link"
                                  nativeButton={false}
                                  className="h-auto justify-start px-0 whitespace-normal wrap-anywhere"
                                  render={
                                    <a
                                      href={toHref(
                                        applicationPath(
                                          selected.stack,
                                          "services",
                                          service.fullName || service.name,
                                        ),
                                      )}
                                    />
                                  }
                                  onClick={(event) =>
                                    followLink(
                                      event,
                                      applicationPath(
                                        selected.stack,
                                        "services",
                                        service.fullName || service.name,
                                      ),
                                    )
                                  }
                                >
                                  {service.name}
                                </Button>
                                <code className="text-xs text-muted-foreground wrap-anywhere">
                                  {service.image}
                                </code>
                              </div>
                            </TableCell>
                            <TableCell>
                              <StatePill
                                label={localizeState(
                                  lang,
                                  serviceRuntimeStatus(service),
                                )}
                                value={serviceRuntimeStatus(service)}
                              />
                            </TableCell>
                            <TableCell>
                              {service.running ?? 0}/{service.desired ?? 0}
                            </TableCell>
                            <TableCell className="whitespace-normal">
                              {(service.nodes || []).join(", ") ||
                                service.node ||
                                "-"}
                            </TableCell>
                            <TableCell>
                              {renderServiceActions(service)}
                            </TableCell>
                          </TableRow>
                        ))}
                        {!selected.services.length ? (
                          <TableRow>
                            <TableCell colSpan={5}>
                              <Empty>
                                <EmptyHeader>
                                  <EmptyTitle>
                                    {lang === "zh" ? "暂无服务" : "No services"}
                                  </EmptyTitle>
                                </EmptyHeader>
                              </Empty>
                            </TableCell>
                          </TableRow>
                        ) : null}
                      </TableBody>
                    </Table>
                  </CardContent>
                </Card>
              ) : (
                activeServices.map((service) => (
                  <Card key={service.fullName || service.name}>
                    <CardHeader>
                      <CardTitle className="wrap-anywhere">
                        {service.name}
                      </CardTitle>
                      <CardDescription>
                        <StatePill
                          label={localizeState(
                            lang,
                            serviceRuntimeStatus(service),
                          )}
                          value={serviceRuntimeStatus(service)}
                        />
                      </CardDescription>
                    </CardHeader>
                    <CardContent>
                      <ApplicationProperties
                        items={[
                          {
                            label: t(lang, "image"),
                            value: (
                              <code className="wrap-anywhere">
                                {service.image || "-"}
                              </code>
                            ),
                          },
                          {
                            label: t(lang, "accessAddress"),
                            value: service.domain || t(lang, "internalOnly"),
                          },
                          {
                            label: t(lang, "replicas"),
                            value: `${service.running ?? 0}/${service.desired ?? 0}`,
                          },
                          {
                            label: t(lang, "nodes"),
                            value:
                              (service.nodes || []).join(", ") ||
                              service.node ||
                              "-",
                          },
                          {
                            label: t(lang, "port"),
                            value: service.targetPort || "-",
                          },
                          {
                            label: t(lang, "network"),
                            value: service.network || "-",
                          },
                        ]}
                      />
                    </CardContent>
                    <CardFooter className="flex-wrap justify-between gap-3">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() =>
                          navigate(applicationPath(selected.stack, "services"))
                        }
                      >
                        {lang === "zh" ? "返回服务列表" : "Back to services"}
                      </Button>
                      {renderServiceActions(service)}
                    </CardFooter>
                  </Card>
                ))
              )}
            </>
          ) : null}
          {tab === "logs" ? (
            <ApplicationLogs
              key={`${selected.stack}:${search}`}
              app={selected.stack}
              lang={lang}
              token={token}
              services={selected.services}
              initialServiceName={
                new URLSearchParams(search).get("service") || ""
              }
              onClose={() =>
                navigate(applicationPath(selected.stack, "overview"))
              }
            />
          ) : null}
          {tab === "metrics" ? (
            <ObservabilityPanel
              key={selected.stack}
              lang={lang}
              token={token}
              services={selected.services}
              nodes={[]}
            />
          ) : null}
        </TabsContent>
      </Tabs>
    </section>
  ) : null;
  const statusItems = [
    { value: "all", label: lang === "zh" ? "全部状态" : "All statuses" },
    ...statusOptions.map((status) => ({
      value: status,
      label: localizeState(lang, status),
    })),
  ];
  const regionItems = [
    { value: "all", label: lang === "zh" ? "全部区域" : "All regions" },
    ...regionOptions.map((region) => ({ value: region, label: region })),
  ];

  return (
    <div className="flex min-w-0 flex-col gap-6" id="section-1">
      {selectedStack && !selected ? (
        <Empty>
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <Box />
            </EmptyMedia>
            <EmptyTitle>
              {lang === "zh" ? "未找到应用" : "Application not found"}
            </EmptyTitle>
            <EmptyDescription>
              {lang === "zh"
                ? `未找到应用 ${selectedStack}，可能已删除或当前账号无法访问。`
                : `Application ${selectedStack} was not found. It may have been removed or is unavailable to this account.`}
            </EmptyDescription>
          </EmptyHeader>
          <EmptyContent>
            <Button variant="outline" onClick={() => setSelected(null)}>
              {lang === "zh" ? "返回列表" : "Back to list"}
            </Button>
          </EmptyContent>
        </Empty>
      ) : null}
      {actionError ? (
        <Alert variant="destructive">
          <AlertCircle />
          <AlertTitle>
            {lang === "zh" ? "操作失败" : "Action failed"}
          </AlertTitle>
          <AlertDescription>{actionError}</AlertDescription>
        </Alert>
      ) : null}
      {actionNotice ? (
        <Alert>
          <AlertDescription>{actionNotice}</AlertDescription>
        </Alert>
      ) : null}
      {actionSteps.length || updatingApp ? (
        <Card aria-live="polite">
          <CardHeader>
            <CardTitle>
              {lang === "zh" ? "应用更新进度" : "Application update progress"}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <StepLog
              steps={actionSteps}
              lang={lang}
              waitingLabel={
                updatingApp
                  ? lang === "zh"
                    ? "正在开始更新…"
                    : "Starting update…"
                  : undefined
              }
            />
          </CardContent>
          {onNavigateToDeployments ? (
            <CardFooter>
              <Button variant="outline" onClick={onNavigateToDeployments}>
                {lang === "zh" ? "查看交付记录" : "View delivery history"}
              </Button>
            </CardFooter>
          ) : null}
        </Card>
      ) : null}
      {!selectedStack ? (
        <>
          <FieldGroup
            className="grid min-w-0 items-end gap-4 md:grid-cols-[minmax(0,1fr)_minmax(10rem,auto)_minmax(10rem,auto)]"
            aria-label={lang === "zh" ? "应用筛选" : "Application filters"}
          >
            <Field className="min-w-0">
              <FieldLabel htmlFor="application-search">
                {lang === "zh" ? "搜索应用" : "Search applications"}
              </FieldLabel>
              <InputGroup>
                <InputGroupAddon>
                  <Search />
                </InputGroupAddon>
                <InputGroupInput
                  id="application-search"
                  value={searchQuery}
                  onChange={(event) => setSearchQuery(event.target.value)}
                  placeholder={
                    lang === "zh"
                      ? "搜索应用、域名、镜像"
                      : "Search app, domain, image"
                  }
                />
              </InputGroup>
            </Field>
            <Field>
              <FieldLabel htmlFor="application-status">
                {t(lang, "status")}
              </FieldLabel>
              <Select
                items={statusItems}
                value={filters.status}
                onValueChange={(value) => {
                  if (value)
                    setFilters((current) => ({ ...current, status: value }));
                }}
              >
                <SelectTrigger id="application-status" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    {statusItems.map((item) => (
                      <SelectItem key={item.value} value={item.value}>
                        {item.label}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                </SelectContent>
              </Select>
            </Field>
            <Field>
              <FieldLabel htmlFor="application-region">
                {t(lang, "region")}
              </FieldLabel>
              <Select
                items={regionItems}
                value={filters.region}
                onValueChange={(value) => {
                  if (value)
                    setFilters((current) => ({ ...current, region: value }));
                }}
              >
                <SelectTrigger id="application-region" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    {regionItems.map((item) => (
                      <SelectItem key={item.value} value={item.value}>
                        {item.label}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                </SelectContent>
              </Select>
            </Field>
          </FieldGroup>
          {filteredApplications.length ? (
            <>
              <Card className="hidden min-w-0 md:flex">
                <CardHeader>
                  <CardTitle>{t(lang, "applications")}</CardTitle>
                  <CardDescription>
                    {lang === "zh"
                      ? `${applicationPage?.total ?? filteredApplications.length} / ${applicationPage?.counts.total ?? applications.length} 个应用`
                      : `${applicationPage?.total ?? filteredApplications.length} / ${applicationPage?.counts.total ?? applications.length} apps`}
                  </CardDescription>
                </CardHeader>
                <CardContent>
                  <Table
                    className="min-w-3xl"
                    containerProps={{
                      tabIndex: 0,
                      role: "region",
                      "aria-label":
                        lang === "zh"
                          ? "应用列表，可横向滚动"
                          : "Applications, horizontally scrollable",
                    }}
                  >
                    <TableHeader>
                      <TableRow>
                        <TableHead>{t(lang, "application")}</TableHead>
                        <TableHead>{t(lang, "status")}</TableHead>
                        <TableHead>{t(lang, "accessAddress")}</TableHead>
                        <TableHead>{t(lang, "region")}</TableHead>
                        <TableHead>{t(lang, "nodes")}</TableHead>
                        <TableHead>{t(lang, "replicas")}</TableHead>
                        <TableHead>{t(lang, "actions")}</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {filteredApplications.map((app) => (
                        <TableRow key={app.stack}>
                          <TableCell className="max-w-72 whitespace-normal">
                            <div className="flex min-w-0 flex-col gap-1">
                              {appLink(app)}
                              <span className="text-xs text-muted-foreground">
                                {serviceCountLabel(app.services.length)}
                              </span>
                            </div>
                          </TableCell>
                          <TableCell>
                            <StatePill
                              label={localizeState(lang, app.status)}
                              value={app.status}
                            />
                          </TableCell>
                          <TableCell className="max-w-64 whitespace-normal">
                            {app.domains.length ? (
                              <div className="flex min-w-0 flex-col gap-1">
                                {app.domains.map((domain) => (
                                  <CodeCell key={domain} value={domain} />
                                ))}
                              </div>
                            ) : (
                              <Badge value={t(lang, "internalOnly")} />
                            )}
                          </TableCell>
                          <TableCell>
                            <BadgeGroup>
                              {app.regions.map((region) => (
                                <Badge key={region} value={region} />
                              ))}
                            </BadgeGroup>
                          </TableCell>
                          <TableCell>
                            <BadgeGroup>
                              {app.nodes.length ? (
                                app.nodes.map((node) => (
                                  <Badge key={node} value={node} />
                                ))
                              ) : (
                                <Badge value="-" />
                              )}
                            </BadgeGroup>
                          </TableCell>
                          <TableCell>
                            <Badge value={`${app.running}/${app.desired}`} />
                          </TableCell>
                          <TableCell>{renderActions(app)}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </CardContent>
              </Card>
              <div className="flex min-w-0 flex-col gap-4 md:hidden">
                {filteredApplications.map((app) => (
                  <Card key={app.stack}>
                    <CardHeader>
                      <CardTitle>{appLink(app)}</CardTitle>
                      <CardDescription>
                        {serviceCountLabel(app.services.length)}
                      </CardDescription>
                      <CardAction>
                        <StatePill
                          label={localizeState(lang, app.status)}
                          value={app.status}
                        />
                      </CardAction>
                    </CardHeader>
                    <CardContent>
                      <ApplicationProperties
                        items={[
                          {
                            label: t(lang, "accessAddress"),
                            value: app.domains.length ? (
                              <div className="flex min-w-0 flex-col gap-1">
                                {app.domains.map((domain) => (
                                  <CodeCell key={domain} value={domain} />
                                ))}
                              </div>
                            ) : (
                              t(lang, "internalOnly")
                            ),
                          },
                          {
                            label: t(lang, "region"),
                            value: app.regions.join(", ") || "-",
                          },
                          {
                            label: t(lang, "nodes"),
                            value: app.nodes.join(", ") || "-",
                          },
                          {
                            label: t(lang, "replicas"),
                            value: `${app.running}/${app.desired}`,
                          },
                        ]}
                      />
                    </CardContent>
                    <CardFooter>{renderActions(app)}</CardFooter>
                  </Card>
                ))}
              </div>
            </>
          ) : (
            <Empty>
              <EmptyHeader>
                <EmptyMedia variant="icon">
                  <Box />
                </EmptyMedia>
                <EmptyTitle>{t(lang, "noApplications")}</EmptyTitle>
                {filters.query ||
                filters.status !== "all" ||
                filters.region !== "all" ? (
                  <EmptyDescription>
                    {lang === "zh"
                      ? "调整搜索词或筛选条件后重试。"
                      : "Try another search or filter."}
                  </EmptyDescription>
                ) : null}
              </EmptyHeader>
              {filters.query ||
              filters.status !== "all" ||
              filters.region !== "all" ? (
                <EmptyContent>
                  <Button
                    variant="outline"
                    onClick={() => {
                      setSearchQuery("");
                      setFilters(() => ({
                        query: "",
                        status: "all",
                        region: "all",
                      }));
                    }}
                  >
                    {lang === "zh" ? "清除筛选" : "Clear filters"}
                  </Button>
                </EmptyContent>
              ) : null}
            </Empty>
          )}
          {applicationPage ? (
            <Pagination
              className="flex flex-wrap items-center justify-between gap-3"
              aria-label={lang === "zh" ? "应用分页" : "Application pagination"}
            >
              <p className="text-sm text-muted-foreground" aria-live="polite">
                {lang === "zh"
                  ? `显示 ${filteredApplications.length ? applicationPage.offset + 1 : 0}–${filteredApplications.length ? applicationPage.offset + filteredApplications.length : 0} / ${applicationPage.total} 个应用`
                  : `Showing ${filteredApplications.length ? applicationPage.offset + 1 : 0}–${filteredApplications.length ? applicationPage.offset + filteredApplications.length : 0} of ${applicationPage.total} apps`}
              </p>
              <PaginationContent>
                <PaginationItem>
                  <Button
                    variant="outline"
                    disabled={applicationPage.offset <= 0}
                    onClick={() =>
                      changePage(
                        Math.max(
                          0,
                          applicationPage.offset - applicationPage.limit,
                        ),
                      )
                    }
                  >
                    {lang === "zh" ? "上一页" : "Previous"}
                  </Button>
                </PaginationItem>
                <PaginationItem>
                  <Button
                    variant="outline"
                    disabled={!applicationPage.hasMore}
                    onClick={() =>
                      changePage(applicationPage.offset + applicationPage.limit)
                    }
                  >
                    {lang === "zh" ? "下一页" : "Next"}
                  </Button>
                </PaginationItem>
              </PaginationContent>
            </Pagination>
          ) : null}
        </>
      ) : null}
      {detailPage}
      {confirmDialog}
    </div>
  );
}
