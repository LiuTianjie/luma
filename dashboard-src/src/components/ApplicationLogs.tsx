import { Info } from "@/components/icons";
import { spaLink, useRouter } from "../router";
import { investigationPath } from "../observeContext";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import type { DashboardService, Lang } from "../types";
import { ObserveAppsPanel } from "./ObserveAppsPanel";
import { ServiceLogsModal } from "./ServiceLogsModal";
import { InvestigationBar } from "./InvestigationBar";

export function ApplicationLogs({
  app,
  services,
  lang,
  token,
  initialServiceName,
  onClose,
  hasObserveLogs = true,
  hasObserveTraces = true,
}: {
  app: string;
  services: DashboardService[];
  lang: Lang;
  token: string;
  initialServiceName: string;
  onClose: () => void;
  hasObserveLogs?: boolean;
  hasObserveTraces?: boolean;
}) {
  const zh = lang === "zh";
  const { path, search, navigate } = useRouter();
  const hasObserve = hasObserveLogs || hasObserveTraces;
  const views = [
    ...(hasObserveLogs ? [{ id: "logs", label: zh ? "日志检索" : "Log search" }] : []),
    ...(hasObserveTraces ? [{ id: "traces", label: zh ? "链路追踪" : "Traces" }] : []),
    { id: "live", label: zh ? "实时日志" : "Live logs" },
  ];
  const allowedIds = views.map((v) => v.id);
  const requested = new URLSearchParams(search).get("logView");
  const defaultMode = !hasObserve || initialServiceName ? "live" : hasObserveLogs ? "logs" : "live";
  const mode = requested && allowedIds.includes(requested) ? requested : defaultMode;
  const setMode = (value: string) => navigate(investigationPath(path, search, { logView: value }));

  return (
    <div className="flex min-w-0 flex-col gap-4">
      {views.length > 1 ? (
        <ToggleGroup
          value={[mode]}
          onValueChange={(values) => {
            if (values[0]) setMode(values[0]);
          }}
          variant="segmented"
          size="sm"
          aria-label={zh ? "日志查看方式" : "Log viewer"}
        >
          {views.map(({ id, label }) => (
            <ToggleGroupItem key={id} value={id}>
              {label}
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
      ) : null}
      {mode === "live" ? (
        <>
          <p className="flex flex-wrap items-center gap-1.5 text-sm text-muted-foreground">
            <span>
              {hasObserve
                ? zh
                  ? "实时日志读取当前实例输出；固定时间范围用于日志检索和 Trace。"
                  : "Live logs read current instance output. Fixed time ranges apply to log search and traces."
                : zh
                  ? "实时日志由 Control 原生提供，直接读取当前容器输出。需要历史日志检索与分布式链路追踪时，可"
                  : "Live container logs are built into Control. For historical log search and distributed traces,"}
            </span>
            {!hasObserve ? (
              <a
                {...spaLink("/addons?install=observe", navigate)}
                className="font-medium text-link underline-offset-4 hover:underline"
              >
                {zh ? "前往插件中心安装 Luma Observe" : "install Luma Observe in Addons Hub"}
              </a>
            ) : null}
          </p>
          <ServiceLogsModal
            inline
            lang={lang}
            token={token}
            services={services}
            initialServiceName={
              initialServiceName ||
              services.find((service) => service.fullName)?.fullName ||
              ""
            }
            onClose={onClose}
          />
        </>
      ) : (
        <>
          <InvestigationBar app={app} lang={lang} />
          <p className="flex items-start gap-2 text-sm text-muted-foreground">
            <Info className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
            <span>
              {zh
                ? (mode === "traces"
                    ? "Tempo 链路追踪，已按当前应用筛选。"
                    : "VictoriaLogs 日志检索，已按当前应用筛选。") +
                  "需启用可观测服务；暂不可用时可切换到实时日志。"
                : (mode === "traces"
                    ? "Tempo traces filtered to this application. "
                    : "VictoriaLogs search filtered to this application. ") +
                  "Requires observability services; live logs remain available separately."}
            </span>
          </p>
          <ObserveAppsPanel
            key={mode}
            lang={lang}
            token={token}
            initialView={mode === "traces" ? "traces" : "logs"}
            initialApp={app}
            lockedView
          />
        </>
      )}
    </div>
  );
}
