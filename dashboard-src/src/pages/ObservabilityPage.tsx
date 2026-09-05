import { AlertingPanel } from "../components/AlertingPanel";
import { StorageGovernancePanel } from "../components/StorageGovernancePanel";
import { useRouter, useSearchParams } from "../router";
import { ObservabilityPanel } from "../components/ObservabilityPanel";
import type { Lang } from "../types";
import type { DashboardViewModel } from "../dashboardViewModel";
import { PageHeader } from "./PageHeader";

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
  const { navigate } = useRouter();
  const query = useSearchParams();
  const selected = query.get("tab") || "metrics";
  const tabs = [
    ["metrics", zh ? "指标与日志" : "Metrics & logs"],
    ["alerts", zh ? "告警中心" : "Incidents"],
    ["rules", zh ? "告警规则" : "Rules"],
    ["notifications", zh ? "通知渠道" : "Notifications"],
    ["storage", zh ? "存储治理" : "Storage governance"],
  ];
  const tab = tabs.some(([key]) => key === selected) ? selected : "metrics";
  const logStreams = vm.services.filter((service) => service.fullName).length;
  return (
    <>
      <PageHeader
        meta={{
          eyebrow: zh ? "运行" : "Operations",
          title: zh ? "可观测性" : "Observability",
          description: zh
            ? "从资源与日志定位问题，配置告警、飞书通知和数据保留策略。"
            : "Investigate resources and logs, configure alerts, Feishu notifications and data retention.",
          metrics: [
            { label: zh ? "节点指标" : "Metric nodes", value: vm.metricNodes },
            { label: zh ? "服务" : "Services", value: vm.services.length },
            { label: zh ? "日志流" : "Log streams", value: logStreams },
            { label: zh ? "刷新" : "Refresh", value: "30s" },
          ],
        }}
      />
      <nav className="observability-tabs" aria-label={zh ? "可观测性模块" : "Observability sections"}>
        {tabs.map(([key, label]) => <button key={key} className="ghost" aria-current={tab === key ? "page" : undefined} onClick={() => navigate(`/observe?tab=${key}`)}>{label}</button>)}
      </nav>
      {tab === "metrics" && <ObservabilityPanel lang={lang} token={token} nodes={vm.nodes} services={vm.services} />}
      {(tab === "alerts" || tab === "rules" || tab === "notifications") && <AlertingPanel key={token} lang={lang} token={token} tab={tab} nodeNames={vm.nodes.flatMap((node) => node.name ? [node.name] : [])} applicationNames={vm.applications.map((app) => app.stack)} />}
      {tab === "storage" && <StorageGovernancePanel lang={lang} token={token} />}
    </>
  );
}
