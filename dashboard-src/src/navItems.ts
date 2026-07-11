import { Activity, Boxes, CloudCog, Hammer, HardDrive, KeyRound, LayoutDashboard, Plus, ScrollText, ServerCog, type LucideIcon } from "lucide-react";
import type { DashboardViewModel, NavPage } from "./dashboardViewModel";
import type { Lang } from "./types";

export type NavItem = {
  id: NavPage;
  icon: LucideIcon;
  label: string;
  value: number;
  detail: string;
};

export type NavGroup = {
  key: string;
  // Uppercase section label shown above the group; null renders no divider (the
  // ungrouped home anchor).
  label: string | null;
  items: NavItem[];
};

// Build the grouped sidebar navigation. Three sections collapse the previously flat
// eight items: RUN (inspect what's running), DELIVER (ship), SETTINGS (rarely-changed
// config). Overview stays ungrouped at the top as the home anchor.
export function buildNavGroups(lang: Lang, vm: DashboardViewModel): NavGroup[] {
  const zh = lang === "zh";
  const overview: NavItem = {
    id: "overview",
    icon: LayoutDashboard,
    label: zh ? "总览" : "Overview",
    value: vm.issueCounts.critical + vm.issueCounts.warning || vm.healthyServices,
    detail: zh ? `${vm.healthyServices}/${vm.services.length} 服务正常` : `${vm.healthyServices}/${vm.services.length} services ok`,
  };
  const apps: NavItem = {
    id: "applications",
    icon: Boxes,
    label: zh ? "应用" : "Apps",
    value: vm.applications.length,
    detail: zh ? "生命周期 · 回滚" : "Lifecycle · rollback",
  };
  const fleet: NavItem = {
    id: "nodes",
    icon: ServerCog,
    label: zh ? "节点" : "Fleet",
    value: vm.nodes.length,
    detail: zh ? `${vm.activeNodes}/${vm.nodes.length} ready · 拓扑` : `${vm.activeNodes}/${vm.nodes.length} ready · topology`,
  };
  const observe: NavItem = {
    id: "observability",
    icon: Activity,
    label: zh ? "观察" : "Observe",
    value: vm.metricNodes,
    detail: zh ? "节点资源 · 日志" : "Resources · logs",
  };
  const storage: NavItem = {
    id: "storage",
    icon: HardDrive,
    label: zh ? "存储" : "Storage",
    value: vm.storageVolumes.length + vm.storageClasses.length,
    detail: zh ? `${vm.storageClasses.length} 类 · ${vm.storageVolumes.length} 卷` : `${vm.storageClasses.length} classes · ${vm.storageVolumes.length} volumes`,
  };
  const builder: NavItem = {
    id: "builder",
    icon: Hammer,
    label: zh ? "构建" : "Builder",
    value: vm.builderNodes,
    detail: zh ? "仓库导入 · 构建历史" : "Repo import · history",
  };
  const deployments: NavItem = {
    id: "deployments",
    icon: ScrollText,
    label: zh ? "部署记录" : "Deployments",
    value: 0,
    detail: zh ? "构建 · CLI · 面板" : "Build · CLI · UI",
  };
  const lae: NavItem = {
    id: "lae",
    icon: CloudCog,
    label: "LAE",
    value: 0,
    detail: zh ? "用户 · 租户 · 应用" : "Users · tenants · apps",
  };
  const create: NavItem = {
    id: "deploy",
    icon: Plus,
    label: zh ? "创建" : "Create",
    value: vm.templateCount,
    detail: zh ? "模板、表单、YAML" : "Templates, form, YAML",
  };
  const credentials: NavItem = {
    id: "credentials",
    icon: KeyRound,
    label: zh ? "凭据" : "Credentials",
    value: vm.storageClasses.length,
    detail: zh ? "Secret · Registry" : "Secrets · registry",
  };

  return [
    { key: "home", label: null, items: [overview] },
    { key: "run", label: zh ? "运行" : "Run", items: [apps, fleet, observe, storage, builder, deployments] },
    { key: "platform", label: zh ? "平台" : "Platform", items: [lae] },
    { key: "deliver", label: zh ? "交付" : "Deliver", items: [create] },
    { key: "settings", label: zh ? "设置" : "Settings", items: [credentials] },
  ];
}
