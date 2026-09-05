import { Activity, Boxes, CloudCog, LayoutDashboard, ScrollText, ServerCog, Settings, type LucideIcon } from "lucide-react";
import type { DashboardViewModel, NavPage } from "./dashboardViewModel";
import type { Lang } from "./types";

export type NavItem = {
  id: NavPage;
  icon: LucideIcon;
  label: string;
  value?: number | null;
  detail: string;
};
export type NavGroup = { key: string; label: string | null; items: NavItem[] };

/** Object workspaces own their secondary pages; badges are reserved for actionable issues. */
export function buildNavGroups(lang: Lang, vm: DashboardViewModel): NavGroup[] {
  const zh = lang === "zh";
  const issues = vm.issueCounts.critical + vm.issueCounts.warning;
  return [
    { key: "workspace", label: zh ? "工作空间" : "Workspace", items: [
      { id: "overview", icon: LayoutDashboard, label: zh ? "总览" : "Overview", value: issues || null, detail: zh ? "运行概况与待处理事项" : "Operations and attention queue" },
      { id: "applications", icon: Boxes, label: zh ? "应用" : "Applications", detail: zh ? "服务、实例、终端与生命周期" : "Services, instances, terminal and lifecycle" },
      { id: "deployments", icon: ScrollText, label: zh ? "交付" : "Delivery", detail: zh ? "构建、部署与任务记录" : "Builds, deployments and tasks" },
      { id: "observability", icon: Activity, label: zh ? "可观测性" : "Observability", detail: zh ? "指标、日志与告警" : "Metrics, logs and alerts" },
      { id: "nodes", icon: ServerCog, label: zh ? "基础设施" : "Infrastructure", detail: zh ? "节点、存储、镜像与网络" : "Nodes, storage, images and networking" },
    ] },
    { key: "platform", label: zh ? "平台管理" : "Platform", items: [
      { id: "lae", icon: CloudCog, label: "LAE", detail: zh ? "用户、租户与应用平台" : "Users, tenants and application platform" },
      { id: "credentials", icon: Settings, label: zh ? "设置" : "Settings", detail: zh ? "凭据与集群管理" : "Credentials and cluster administration" },
    ] },
  ];
}
