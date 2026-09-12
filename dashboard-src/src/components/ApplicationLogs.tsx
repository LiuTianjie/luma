import { useState } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import type { DashboardService, Lang } from "../types";
import { ObserveAppsPanel } from "./ObserveAppsPanel";
import { ServiceLogsModal } from "./ServiceLogsModal";

export function ApplicationLogs({
  app,
  services,
  lang,
  token,
  initialServiceName,
  onClose,
}: {
  app: string;
  services: DashboardService[];
  lang: Lang;
  token: string;
  initialServiceName: string;
  onClose: () => void;
}) {
  const zh = lang === "zh";
  const [mode, setMode] = useState(initialServiceName ? "live" : "logs");
  const views = [
    { id: "logs", label: zh ? "日志检索" : "Log search" },
    { id: "traces", label: zh ? "链路追踪" : "Traces" },
    { id: "live", label: zh ? "实时日志" : "Live logs" },
  ];
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <ToggleGroup
        value={[mode]}
        onValueChange={(values) => {
          if (values[0]) setMode(values[0]);
        }}
        variant="outline"
        size="sm"
        className="flex-wrap"
        aria-label={zh ? "日志查看方式" : "Log viewer"}
      >
        {views.map(({ id, label }) => (
          <ToggleGroupItem key={id} value={id}>
            {label}
          </ToggleGroupItem>
        ))}
      </ToggleGroup>
      {mode === "live" ? (
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
      ) : (
        <>
          <Alert>
            <AlertDescription>
              {zh
                ? (mode === "traces"
                    ? "Tempo 链路追踪，已按当前应用筛选。"
                    : "VictoriaLogs 日志检索，已按当前应用筛选。") +
                  "需启用可观测服务；暂不可用时可切换到实时日志。"
                : (mode === "traces"
                    ? "Tempo traces filtered to this application. "
                    : "VictoriaLogs search filtered to this application. ") +
                  "Requires observability services; live logs remain available separately."}
            </AlertDescription>
          </Alert>
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
