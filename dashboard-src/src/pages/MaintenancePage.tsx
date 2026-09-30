import { SystemUpdatePanel } from "../components/SystemUpdatePanel";
import type { Lang } from "../types";
import type { DashboardViewModel } from "../dashboardViewModel";
import { PageHeader } from "./PageHeader";
import "./InfrastructureWorkspace.css";

// Upgrades target Luma itself (Control and node agents), so maintenance lives with
// platform settings rather than inside the fleet inventory.
export function MaintenancePage({
  lang,
  token,
  vm,
  controlVersion,
  onRefresh,
}: {
  lang: Lang;
  token: string;
  vm: DashboardViewModel;
  controlVersion: string;
  onRefresh: () => Promise<void> | void;
}) {
  const zh = lang === "zh";
  return (
    <div className="infrastructure-workspace">
      <PageHeader
        meta={{
          eyebrow: zh ? "设置" : "Settings",
          title: zh ? "系统维护" : "System maintenance",
          description: zh ? "检查路由，升级控制面和节点，并追踪任务结果。" : "Check routes, upgrade the control plane and agents, and track results.",
          metrics: [],
        }}
      />
      <SystemUpdatePanel lang={lang} token={token} controlVersion={controlVersion} nodes={vm.nodes} onRefresh={onRefresh} />
    </div>
  );
}
