import { Activity, Boxes, LayoutDashboard, ScrollText, ServerCog, Settings, WandSparkles, type LucideIcon } from "lucide-react";
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
/**
 * A secondary destination. `exact` stops the href from claiming nested paths;
 * `also` lists extra path prefixes (task pages, object details) that belong to it.
 */
export type NavChild = { href: string; label: string; detail: string; exact?: boolean; also?: string[] };
export type NavGroup = { key: string; label: string | null; items: NavItem[] };

// Pages without their own sidebar entry highlight the workspace that owns them.
const WORKSPACE_BY_PAGE: Partial<Record<NavPage, NavPage>> = {
  deploy: "applications",
  builder: "applications",
  storage: "nodes",
  registry: "nodes",
  maintenance: "credentials",
};

export function navWorkspace(page: NavPage): NavPage {
  return WORKSPACE_BY_PAGE[page] || page;
}

function matchLength(prefix: string, path: string): number {
  return path === prefix || path.startsWith(`${prefix}/`) ? prefix.length : -1;
}

/** The single secondary page that owns `path`; the most specific match wins. */
export function activeNavChild(item: NavItem | undefined, path: string): NavChild | undefined {
  let best: NavChild | undefined;
  let bestLength = -1;
  for (const child of item?.children || []) {
    const own = child.exact ? (path === child.href ? child.href.length : -1) : matchLength(child.href, path);
    const length = Math.max(own, ...(child.also || []).map((prefix) => matchLength(prefix, path)));
    if (length > bestLength) {
      best = child;
      bestLength = length;
    }
  }
  return best;
}

/** Object workspaces own their secondary pages; badges are reserved for actionable issues. */
export function buildNavGroups(lang: Lang, vm: DashboardViewModel): NavGroup[] {
  const zh = lang === "zh";
  const issues = vm.issueCounts.critical + vm.issueCounts.warning;
  return [
    { key: "workspace", label: zh ? "工作空间" : "Workspace", items: [
      { id: "overview", icon: LayoutDashboard, label: zh ? "总览" : "Overview", value: issues || null, detail: zh ? "运行概况与待处理事项" : "Operations and attention queue" },
      { id: "applications", icon: Boxes, label: zh ? "应用" : "Applications", detail: zh ? "服务、实例、终端与生命周期" : "Services, instances, terminal and lifecycle" },
      { id: "deployments", icon: ScrollText, label: zh ? "部署记录" : "Deployments", detail: zh ? "构建、部署与任务记录" : "Builds, deployments and tasks" },
      { id: "observability", icon: Activity, label: zh ? "可观测" : "Observability", detail: zh ? "指标、日志与告警" : "Metrics, logs and alerts", children: [
        { href: "/observe", label: zh ? "告警事件" : "Incidents", detail: zh ? "当前触发与历史告警" : "Active and historical incidents" },
        { href: "/observe/apps", label: zh ? "应用监控" : "App monitoring", detail: zh ? "按应用查看请求、日志与链路" : "Requests, logs and traces by app", also: ["/observe/logs"] },
        { href: "/observe/metrics", label: zh ? "资源指标" : "Metrics", detail: zh ? "节点与服务资源曲线" : "Node and service metrics" },
        { href: "/observe/rules", label: zh ? "告警规则" : "Rules", detail: zh ? "配置触发条件" : "Configure alert conditions" },
        { href: "/observe/channels", label: zh ? "通知渠道" : "Channels", detail: zh ? "管理通知出口" : "Manage notification destinations" },
      ] },
      { id: "nodes", icon: ServerCog, label: zh ? "基础设施" : "Infrastructure", detail: zh ? "节点、网络、存储与镜像" : "Nodes, networking, storage and images", children: [
        { href: "/fleet", label: zh ? "节点" : "Nodes", detail: zh ? "节点资源、调度状态与加入新节点" : "Node resources, scheduling and joining", exact: true, also: ["/fleet/join", "/fleet/nodes", "/terminal/node"] },
        { href: "/fleet/regions", label: zh ? "区域" : "Regions", detail: zh ? "调度区域与出口策略" : "Scheduling regions and egress" },
        { href: "/fleet/network", label: zh ? "网络" : "Network", detail: zh ? "入口路径与节点拓扑" : "Ingress paths and topology" },
        { href: "/storage", label: zh ? "存储" : "Storage", detail: zh ? "卷、存储类、容量与回收" : "Volumes, classes, capacity and cleanup" },
        { href: "/registry", label: zh ? "镜像" : "Registry", detail: zh ? "镜像生命周期与清理" : "Image lifecycle and cleanup" },
      ] },
    ] },
    { key: "platform", label: zh ? "平台管理" : "Platform", items: [
      { id: "credentials", icon: Settings, label: zh ? "设置" : "Settings", detail: zh ? "凭据与控制面维护" : "Credentials and control-plane maintenance", children: [
        { href: "/settings/secrets", label: zh ? "密钥" : "Secrets", detail: zh ? "管理控制面密钥" : "Manage control-plane secrets" },
        { href: "/settings/registries", label: zh ? "镜像仓库凭据" : "Registry credentials", detail: zh ? "私有镜像拉取凭据" : "Private image pull credentials" },
        { href: "/settings/git", label: zh ? "Git 凭据" : "Git credentials", detail: zh ? "代码仓库访问凭据" : "Source control credentials" },
        { href: "/settings/maintenance", label: zh ? "系统维护" : "Maintenance", detail: zh ? "升级控制面与节点、检查路由" : "Upgrades and route checks" },
      ] },
      { id: "setup", icon: WandSparkles, label: zh ? "首次安装" : "First install", detail: zh ? "配置依赖并验证集群" : "Configure dependencies and verify the cluster" },
    ] },
  ];
}
