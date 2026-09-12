import { useState } from "react";
import { Button } from "@/components/ui/button";
import type { DashboardService, Lang } from "../types";
import { ObserveAppsPanel } from "./ObserveAppsPanel";
import { ServiceLogsModal } from "./ServiceLogsModal";

export function ApplicationLogs({ app, services, lang, token, initialServiceName, onClose }: {
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
  return <div className="application-logs grid gap-3">
    <div className="flex flex-wrap gap-1" role="group" aria-label={zh ? "日志查看方式" : "Log viewer"}>
      {views.map(({ id, label }) => <Button key={id} size="sm" variant={mode === id ? "secondary" : "ghost"} aria-pressed={mode === id} onClick={() => setMode(id)}>{label}</Button>)}
    </div>
    {mode === "live" ? <ServiceLogsModal inline lang={lang} token={token} services={services}
      initialServiceName={initialServiceName || services.find((service) => service.fullName)?.fullName || ""} onClose={onClose} /> : <>
      <p className="text-xs text-muted-foreground">{zh
        ? (mode === "traces" ? "Tempo 链路追踪，已按当前应用筛选。" : "VictoriaLogs 日志检索，已按当前应用筛选。") + "需启用可观测服务；暂不可用时可切换到实时日志。"
        : (mode === "traces" ? "Tempo traces filtered to this application. " : "VictoriaLogs search filtered to this application. ") + "Requires observability services; live logs remain available separately."}</p>
      <ObserveAppsPanel key={mode} lang={lang} token={token} initialView={mode === "traces" ? "traces" : "logs"} initialApp={app} lockedView />
    </>}
  </div>;
}
