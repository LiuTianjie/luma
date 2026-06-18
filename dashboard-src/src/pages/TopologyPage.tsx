import { NodeTopology } from "../components/NodeTopology";
import { TrafficPaths } from "../components/TrafficPaths";
import { t } from "../i18n";
import type { Lang } from "../types";
import type { DashboardViewModel } from "../dashboardViewModel";
import { PageHeader } from "./PageHeader";

export function TopologyPage({
  lang,
  token,
  vm,
  onRefresh,
}: {
  lang: Lang;
  token: string;
  vm: DashboardViewModel;
  onRefresh: () => Promise<void> | void;
}) {
  const zh = lang === "zh";
  return (
    <>
      <PageHeader
        meta={{
          eyebrow: zh ? "拓扑视图" : "Topology",
          title: zh ? "节点拓扑与流量路径" : "Node topology and traffic paths",
          description: zh
            ? "按入口、代理、服务和节点梳理真实流向，快速定位路径断点。"
            : "Trace real ingress, proxy, service, and node placement to spot route breaks quickly.",
          metrics: [
            { label: t(lang, "nodes"), value: `${vm.activeNodes}/${vm.nodes.length}` },
            { label: t(lang, "services"), value: vm.services.length },
            { label: t(lang, "trafficPaths"), value: vm.trafficPaths.length },
          ],
        }}
      />
      <TrafficPaths lang={lang} paths={vm.trafficPaths} theme="dark" token={token} onRefresh={onRefresh} />
      <NodeTopology lang={lang} nodes={vm.nodes} services={vm.services} theme="dark" />
    </>
  );
}
