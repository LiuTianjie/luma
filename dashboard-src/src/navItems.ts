import { Activity, Boxes, CloudCog, LayoutDashboard, ScrollText, ServerCog, Settings, WandSparkles, type LucideIcon } from "lucide-react";
import type { DashboardViewModel, NavPage } from "./dashboardViewModel";
import type { Lang } from "./types";

export type NavItem = {
  id: NavPage;
  icon: LucideIcon;
  label: string;
  value?: number | null;
  detail: string;
  children?: NavChild[];
};
export type NavChild = { href: string; label: string; detail: string };
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
      { id: "observability", icon: Activity, label: zh ? "可观测性" : "Observability", detail: zh ? "指标、日志与告警" : "Metrics, logs and alerts", children: [
        { href: "/observe", label: zh ? "告警事件" : "Incidents", detail: zh ? "当前触发与历史告警" : "Active and historical incidents" },
        { href: "/observe/apps", label: zh ? "应用" : "Apps", detail: zh ? "按应用查看请求与日志" : "Requests and logs by app" },
        { href: "/observe/metrics", label: zh ? "资源指标" : "Metrics", detail: zh ? "节点与服务资源曲线" : "Node and service metrics" },
        { href: "/observe/logs", label: zh ? "日志" : "Logs", detail: zh ? "应用日志与检索" : "Application logs and search" },
        { href: "/observe/rules", label: zh ? "告警规则" : "Rules", detail: zh ? "配置触发条件" : "Configure alert conditions" },
        { href: "/observe/channels", label: zh ? "通知渠道" : "Channels", detail: zh ? "管理通知出口" : "Manage notification destinations" },
      ] },
      { id: "nodes", icon: ServerCog, label: zh ? "基础设施" : "Infrastructure", detail: zh ? "节点、存储、镜像与网络" : "Nodes, storage, images and networking", children: [
        { href: "/fleet", label: zh ? "节点" : "Nodes", detail: zh ? "节点资源与调度状态" : "Node resources and scheduling" },
        { href: "/fleet/join", label: zh ? "加入节点" : "Join node", detail: zh ? "接入新的运行节点" : "Connect a new runtime node" },
        { href: "/fleet/regions", label: zh ? "区域" : "Regions", detail: zh ? "调度区域与出口策略" : "Scheduling regions and egress" },
        { href: "/fleet/maintenance", label: zh ? "系统维护" : "Maintenance", detail: zh ? "升级与路由检查" : "Upgrades and route checks" },
        { href: "/storage", label: zh ? "存储" : "Storage", detail: zh ? "卷与存储类" : "Volumes and storage classes" },
        { href: "/registry", label: zh ? "镜像" : "Registry", detail: zh ? "镜像生命周期与清理" : "Image lifecycle and cleanup" },
        { href: "/fleet/network", label: zh ? "网络" : "Network", detail: zh ? "入口路径与节点拓扑" : "Ingress paths and topology" },
      ] },
      { id: "setup", icon: WandSparkles, label: zh ? "首次安装" : "First install", detail: zh ? "配置依赖并验证集群" : "Configure dependencies and verify the cluster" },
    ] },
    { key: "platform", label: zh ? "平台管理" : "Platform", items: [
      { id: "lae", icon: CloudCog, label: "LAE", detail: zh ? "用户、租户与应用平台" : "Users, tenants and application platform" },
      { id: "credentials", icon: Settings, label: zh ? "设置" : "Settings", detail: zh ? "凭据与集群管理" : "Credentials and cluster administration", children: [
        { href: "/settings/secrets", label: zh ? "密钥" : "Secrets", detail: zh ? "管理控制面密钥" : "Manage control-plane secrets" },
        { href: "/settings/registries", label: zh ? "镜像仓库凭据" : "Registry credentials", detail: zh ? "私有镜像拉取凭据" : "Private image pull credentials" },
        { href: "/settings/git", label: "Git", detail: zh ? "代码仓库访问凭据" : "Source control credentials" },
        { href: "/settings/storage", label: zh ? "存储配置" : "Storage config", detail: zh ? "存储类与节点配置" : "Storage classes and node config" },
        { href: "/settings/maintenance", label: zh ? "维护" : "Maintenance", detail: zh ? "控制面维护操作" : "Control-plane maintenance" },
      ] },
    ] },
  ];
}
