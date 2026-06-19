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
  const logStreams = vm.services.filter((service) => service.fullName).length;
  return (
    <>
      <PageHeader
        meta={{
          eyebrow: zh ? "观察" : "Observability",
          title: zh ? "资源趋势、任务与日志" : "Resource trends, tasks, and logs",
          description: zh
            ? "查看节点资源、服务实际资源、任务状态和日志流；历史曲线按控制面采样刷新。"
            : "Inspect node resources, service actuals, task state, and log streams. History charts refresh on the control-plane sample cadence.",
          metrics: [
            { label: zh ? "节点指标" : "Metric nodes", value: vm.metricNodes },
            { label: zh ? "服务" : "Services", value: vm.services.length },
            { label: zh ? "日志流" : "Log streams", value: logStreams },
            { label: zh ? "刷新" : "Refresh", value: "30s" },
          ],
        }}
      />
      <ObservabilityPanel lang={lang} token={token} nodes={vm.nodes} services={vm.services} />
    </>
  );
}
