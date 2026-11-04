import { ObservationStatus } from "../components/ObservationStatus";
import { useEffect, useMemo } from "react";
import { AlertingPanel } from "../components/AlertingPanel";
import { useRouter, useSearchParams } from "../router";
import { ObservabilityPanel } from "../components/ObservabilityPanel";
import { ObserveAppsPanel } from "../components/ObserveAppsPanel";
import { ObserveSettingsPanel } from "../components/ObserveSettingsPanel";
import { createObserveRefreshStore, ObserveRefreshControl, ObserveRefreshProvider } from "../components/observeRefresh";
import type { Lang } from "../types";
import type { DashboardViewModel } from "../dashboardViewModel";
import { PageHeader } from "./PageHeader";

type Section = { title: [string, string]; description: [string, string]; interval?: number; refresh?: boolean };

// `logs` is a view of application monitoring, so it shares that page's name with
// the sidebar entry and breadcrumb.
const SECTIONS: Record<string, Section> = {
  incidents: {
    title: ["告警事件", "Alert incidents"],
    description: ["Control 规则与 Alertmanager 产生的告警。在这里确认影响、停止提醒或关闭事件。", "Incidents from Control rules and Alertmanager. Review impact, stop reminders or close incidents."],
    interval: 15,
    refresh: true,
  },
  status: {
    title: ["平台状态", "Platform health"],
    description: ["指标、日志与链路的采集和查询是否正常。没有采样不代表系统健康。", "Whether metrics, logs and traces are being collected and queried. Missing samples do not establish health."],
    refresh: true,
  },
  apps: {
    title: ["应用监控", "Application monitoring"],
    description: ["HTTP 入口、Nomad 任务、节点看板，以及链路与日志检索。", "HTTP ingress, Nomad job and node dashboards, with trace and log search."],
    refresh: true,
  },
  metrics: {
    title: ["资源指标", "Resource metrics"],
    description: ["节点与服务的 CPU、内存和磁盘用量历史。", "CPU, memory and disk history for nodes and services."],
    interval: 30,
    refresh: true,
  },
  rules: {
    title: ["告警规则", "Alert rules"],
    description: ["条件持续满足后触发，恢复时通知。未分配渠道的规则只记录告警。", "Rules fire after a condition holds and notify on recovery. Rules without channels only record incidents."],
    interval: 15,
    refresh: true,
  },
  channels: {
    title: ["通知渠道", "Notification channels"],
    description: ["告警推送目标与最近的发送记录。", "Where alerts are delivered, and recent delivery attempts."],
    interval: 15,
    refresh: true,
  },
  settings: {
    title: ["观测设置", "Observability settings"],
    description: ["观测平台的共享与访问选项。", "Sharing and access options for the observability stack."],
  },
};
SECTIONS.logs = SECTIONS.apps;

export function ObservabilityPage({
  lang,
  token,
  vm,
}: {
  lang: Lang;
  token: string;
  vm: DashboardViewModel;
}) {
  const zh = lang === "zh";
  const { path, navigate } = useRouter();
  const query = useSearchParams();
  const legacy = query.get("tab") || query.get("view");
  const legacyPath =
    legacy === "storage"
      ? "/storage/governance"
      : legacy === "notifications"
        ? "/observe/channels"
        : legacy === "alerts"
          ? "/observe"
          : legacy
            ? `/observe/${legacy}`
            : "";
  useEffect(() => {
    if (path === "/observe" && legacyPath) {
      const next = new URLSearchParams(query);
      next.delete("tab");
      next.delete("view");
      navigate(`${legacyPath}${next.size ? `?${next}` : ""}`, {
        replace: true,
      });
    }
  }, [path, legacyPath, navigate, query]);
  const section = path.split("/")[2] || "incidents";
  const meta = SECTIONS[section] || SECTIONS.incidents;
  // A fresh store per section, so one page's "updated at" never shows on another.
  const refreshStore = useMemo(() => createObserveRefreshStore(), [section]);
  // Editors and incident details carry their own heading and back action.
  const subPage = Boolean(path.split("/")[3]);
  return (
    <ObserveRefreshProvider value={refreshStore}>
      <div className="observe-workspace flex min-w-0 flex-col gap-6">
        <PageHeader
          meta={{
            eyebrow: "",
            title: meta.title[zh ? 0 : 1],
            metrics: [],
            description: subPage ? "" : meta.description[zh ? 0 : 1],
            action: meta.refresh && !subPage ? <ObserveRefreshControl lang={lang} intervalSeconds={meta.interval} /> : undefined,
          }}
        />
        {section === "status" ? <ObservationStatus lang={lang} token={token} /> : section === "settings" ? (
          <ObserveSettingsPanel key={token} lang={lang} token={token} />
        ) : section === "apps" || section === "logs" ? (
          <ObserveAppsPanel
            lang={lang}
            token={token}
            applicationNames={vm.applications.map((app) => app.stack)}
            initialView={section === "logs" ? "logs" : "http"}
            initialApp={query.get("app") || ""}
          />
        ) : section === "metrics" ? (
          <ObservabilityPanel
            lang={lang}
            token={token}
            nodes={vm.nodes}
            services={vm.services}
            mode="metrics"
          />
        ) : (
          <AlertingPanel
            key={`${token}:${section}:${query.get("app") || ""}`}
            lang={lang}
            token={token}
            tab={
              section === "rules"
                ? "rules"
                : section === "channels"
                  ? "notifications"
                  : "alerts"
            }
            nodeNames={vm.nodes.flatMap((node) => (node.name ? [node.name] : []))}
            applicationNames={vm.applications.map((app) => app.stack)}
          />
        )}
      </div>
    </ObserveRefreshProvider>
  );
}
