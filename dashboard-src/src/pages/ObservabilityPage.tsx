import { useEffect } from "react";
import { AlertingPanel } from "../components/AlertingPanel";
import { useRouter, useSearchParams } from "../router";
import { ObservabilityPanel } from "../components/ObservabilityPanel";
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
  const tabs = [["incidents", "/observe", zh ? "告警事件" : "Incidents"], ["metrics", "/observe/metrics", zh ? "资源指标" : "Metrics"], ["logs", "/observe/logs", zh ? "实时日志" : "Logs"], ["rules", "/observe/rules", zh ? "告警规则" : "Rules"], ["channels", "/observe/channels", zh ? "通知渠道" : "Channels"]];
  return <>
    <PageHeader meta={{ eyebrow: zh ? "运行保障" : "Operations", title: zh ? "可观测性" : "Observability", metrics: [], description: zh ? "从告警定位对象，查看指标与实时日志。" : "Investigate incidents with resource metrics and live logs." }} />
    <nav className="observability-tabs" aria-label={zh ? "可观测性模块" : "Observability sections"}>{tabs.map(([key, to, label]) => <button key={key} className="ghost" aria-current={section === key ? "page" : undefined} onClick={() => navigate(to)}>{label}</button>)}</nav>
    {(section === "metrics" || section === "logs") ? <ObservabilityPanel lang={lang} token={token} nodes={vm.nodes} services={vm.services} mode={section} /> : <AlertingPanel key={token} lang={lang} token={token} tab={section === "rules" ? "rules" : section === "channels" ? "notifications" : "alerts"} nodeNames={vm.nodes.flatMap((node) => node.name ? [node.name] : [])} applicationNames={vm.applications.map((app) => app.stack)} />}
  </>;
}
