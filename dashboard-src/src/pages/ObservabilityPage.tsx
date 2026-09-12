import { useEffect } from "react";
import { AlertingPanel } from "../components/AlertingPanel";
import { useRouter, useSearchParams } from "../router";
import { ObservabilityPanel } from "../components/ObservabilityPanel";
import { ObserveAppsPanel } from "../components/ObserveAppsPanel";
import type { Lang } from "../types";
import type { DashboardViewModel } from "../dashboardViewModel";
import { PageHeader } from "./PageHeader";

export function ObservabilityPage({ lang, token, vm }: { lang: Lang; token: string; vm: DashboardViewModel }) {
  const zh = lang === "zh";
  const { path, navigate } = useRouter();
  const query = useSearchParams();
  const legacy = query.get("tab") || query.get("view");
  const legacyPath = legacy === "storage" ? "/storage/governance" : legacy === "notifications" ? "/observe/channels" : legacy === "alerts" ? "/observe" : legacy ? `/observe/${legacy}` : "";
  useEffect(() => { if (path === "/observe" && legacyPath) { const next = new URLSearchParams(query); next.delete("tab"); next.delete("view"); navigate(`${legacyPath}${next.size ? `?${next}` : ""}`, { replace: true }); } }, [path, legacyPath, navigate, query]);
  const section = path.split("/")[2] || "incidents";
  const titles: Record<string, [string, string]> = { incidents: ["告警事件", "Alert incidents"], apps: ["应用监控", "Application monitoring"], logs: ["日志", "Logs"], metrics: ["资源指标", "Resource metrics"], rules: ["告警规则", "Alert rules"], channels: ["通知渠道", "Notification channels"] };
  const title = titles[section] || titles.incidents;
  return <div className="observe-workspace">
    <PageHeader meta={{ eyebrow: "", title: title[zh ? 0 : 1], metrics: [], description: "" }} />
    {section === "apps" || section === "logs" ? <ObserveAppsPanel lang={lang} token={token} applicationNames={vm.applications.map((app) => app.stack)} initialView={section === "logs" ? "logs" : "http"} initialApp={query.get("app") || ""} /> : section === "metrics" ? <ObservabilityPanel lang={lang} token={token} nodes={vm.nodes} services={vm.services} mode="metrics" /> : <AlertingPanel key={`${token}:${section}`} lang={lang} token={token} tab={section === "rules" ? "rules" : section === "channels" ? "notifications" : "alerts"} nodeNames={vm.nodes.flatMap((node) => node.name ? [node.name] : [])} applicationNames={vm.applications.map((app) => app.stack)} />}
  </div>;
}
