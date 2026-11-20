import { Link, useLocation, useNavigate } from "react-router";
import {
  Activity,
  FingerprintPattern,
  HardDrive,
  LayoutDashboard,
  PanelLeftIcon,
  Plus,
  ScrollText,
  Settings2,
  TriangleAlert,
  Zap,
} from "@/components/icons";
import { Filter, Gauge, Users } from "lucide-react";
import lumaLogoMark from "@/assets/luma-logo-mark.png";
import type { Project, RetentionStatus } from "@/api/types";
import { SelectControl } from "@/components/primitives";
import { Button } from "@/components/ui/button";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuBadge,
  SidebarMenuButton,
  SidebarMenuItem,
  useSidebar,
} from "@/components/ui/sidebar";
import { cn } from "@/lib/utils";
import { paths, type ProjectRouteSegment } from "@/router/paths";
import { formatBytes } from "@/utils/format";

const isMac =
  typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);

const PROJECT_NAV: Array<{
  segment: ProjectRouteSegment;
  label: string;
  icon: (props: { className?: string }) => React.ReactNode;
}> = [
  { segment: "overview", label: "流量与地域概览", icon: (p) => <Activity {...p} /> },
  { segment: "sessions", label: "用户会话与轨迹", icon: (p) => <Users {...p} /> },
  { segment: "events", label: "实时事件流", icon: (p) => <Zap {...p} /> },
  { segment: "issues", label: "异常与黑匣子", icon: (p) => <TriangleAlert {...p} /> },
  { segment: "performance", label: "Web Vitals 性能", icon: (p) => <Gauge {...p} /> },
  { segment: "funnel", label: "转化漏斗与留存", icon: (p) => <Filter {...p} /> },
  { segment: "logs", label: "客户端日志", icon: (p) => <ScrollText {...p} /> },
  { segment: "settings", label: "接入与项目设置", icon: (p) => <Settings2 {...p} /> },
];

export function AppSidebar({
  projects,
  activeProjectSlug,
  since,
  openIssuesCount = 0,
  retention,
  onOpenNewProject,
}: {
  projects: Project[];
  activeProjectSlug: string;
  since: string;
  openIssuesCount?: number;
  retention?: RetentionStatus;
  onOpenNewProject: () => void;
}) {
  const location = useLocation();
  const navigate = useNavigate();
  const { open, isMobile, setOpenMobile, toggleSidebar } = useSidebar();

  const closeMobile = () => {
    if (isMobile) setOpenMobile(false);
  };

  const pathname = location.pathname;
  const isPortfolio = pathname === "/portfolio" || pathname === "/";
  const isAccount = pathname === "/account";

  const activeSegment: ProjectRouteSegment | null = (() => {
    const segs = pathname.split("/").filter(Boolean);
    if (segs[0] !== "projects" || !segs[2]) return null;
    if (segs[2] === "users") return "sessions";
    return segs[2] as ProjectRouteSegment;
  })();

  const usedBytes = retention?.total_used_bytes || 0;
  const maxBytes = retention?.max_storage_bytes || 15 * 1024 * 1024 * 1024;
  const usedPct = maxBytes > 0 ? Math.min(100, (usedBytes * 100) / maxBytes) : 0;
  const storageTone = usedPct >= 90 ? "bg-destructive" : usedPct >= 75 ? "bg-warning" : "bg-success";
  const collapseLabel = open ? "收起侧边栏" : "展开侧边栏";

  return (
    <Sidebar collapsible="icon" variant="sidebar">
      <SidebarHeader className="h-(--console-header-height) shrink-0 justify-center border-b border-sidebar-border px-3 group-data-[collapsible=icon]:px-2">
        <Link
          to={paths.portfolio(since)}
          onClick={closeMobile}
          className="flex min-w-0 items-center gap-2.5 rounded-md px-1 py-1 outline-none focus-visible:ring-2 focus-visible:ring-ring group-data-[collapsible=icon]:justify-center group-data-[collapsible=icon]:px-0"
          aria-label="Luma Eye 控制台首页"
        >
          <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-background shadow-xs ring-1 ring-border">
            <img src={lumaLogoMark} alt="" className="size-5" width={20} height={20} />
          </span>
          <span className="flex min-w-0 flex-col gap-0.5 leading-none group-data-[collapsible=icon]:hidden">
            <strong className="text-[0.9375rem] font-semibold tracking-[-0.01em]">Luma Eye</strong>
            <span className="truncate text-[0.6875rem] text-muted-foreground">可观测性与行为分析</span>
          </span>
        </Link>
      </SidebarHeader>

      <SidebarContent className="gap-0 px-2.5 pt-3 pb-2 group-data-[collapsible=icon]:px-2">
        <SidebarGroup className="px-0 py-1">
          <SidebarGroupLabel className="h-6 px-2.5 text-[0.6875rem] font-medium tracking-wide text-subtle-foreground uppercase">
            全局工作区
          </SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu className="gap-px">
              <SidebarMenuItem>
                <SidebarMenuButton
                  isActive={isPortfolio}
                  tooltip="多项目总览"
                  render={
                    <Link
                      to={paths.portfolio(since)}
                      aria-current={isPortfolio ? "page" : undefined}
                      onClick={closeMobile}
                    />
                  }
                >
                  <LayoutDashboard />
                  <span className="truncate">多项目总览</span>
                </SidebarMenuButton>
              </SidebarMenuItem>
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>

        <SidebarGroup className="px-0 py-1.5">
          <div className="mb-1.5 flex items-center justify-between px-2.5 group-data-[collapsible=icon]:hidden">
            <span className="text-[0.6875rem] font-medium tracking-wide text-subtle-foreground uppercase">
              监控项目 ({projects.length})
            </span>
            <Button
              type="button"
              variant="ghost"
              size="icon-xs"
              onClick={onOpenNewProject}
              title="新建监控项目"
              aria-label="新建监控项目"
            >
              <Plus className="size-3.5" />
            </Button>
          </div>

          {projects.length > 0 ? (
            <div className="mb-2 px-1.5 group-data-[collapsible=icon]:hidden">
              <SelectControl
                ariaLabel="切换当前监控项目"
                value={activeProjectSlug}
                options={projects.map((p) => ({
                  value: p.slug,
                  label: p.name && p.name !== p.slug ? `${p.name} (${p.slug})` : p.slug,
                }))}
                onChange={(nextSlug) => {
                  const targetSeg = activeSegment || "overview";
                  navigate(paths.projectSection(nextSlug, targetSeg, since));
                  closeMobile();
                }}
              />
            </div>
          ) : null}

          {activeProjectSlug ? (
            <SidebarGroupContent>
              <SidebarMenu className="gap-px">
                {PROJECT_NAV.map((item) => {
                  const active = activeSegment === item.segment && !isPortfolio && !isAccount;
                  const href = paths.projectSection(activeProjectSlug, item.segment, since);
                  const showBadge = item.segment === "issues" && openIssuesCount > 0;
                  return (
                    <SidebarMenuItem key={item.segment}>
                      <SidebarMenuButton
                        isActive={active}
                        tooltip={item.label}
                        render={
                          <Link
                            to={href}
                            aria-current={active ? "page" : undefined}
                            onClick={closeMobile}
                          />
                        }
                      >
                        {item.icon({ className: "size-4 shrink-0" })}
                        <span className="truncate">{item.label}</span>
                      </SidebarMenuButton>
                      {showBadge ? (
                        <SidebarMenuBadge
                          className="top-1.5! right-2 h-5 min-w-5 rounded-full bg-warning-soft px-1.5 text-[0.6875rem] text-warning-foreground!"
                          aria-label={`${openIssuesCount} 项待处理异常`}
                        >
                          {openIssuesCount}
                        </SidebarMenuBadge>
                      ) : null}
                    </SidebarMenuItem>
                  );
                })}
              </SidebarMenu>
            </SidebarGroupContent>
          ) : null}
        </SidebarGroup>

        <SidebarGroup className="mt-auto px-0 py-1.5">
          <SidebarGroupLabel className="h-6 px-2.5 text-[0.6875rem] font-medium tracking-wide text-subtle-foreground uppercase">
            系统与安全
          </SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu className="gap-px">
              <SidebarMenuItem>
                <SidebarMenuButton
                  isActive={isAccount}
                  tooltip="登录与 Passkey"
                  render={
                    <Link
                      to={paths.account()}
                      aria-current={isAccount ? "page" : undefined}
                      onClick={closeMobile}
                    />
                  }
                >
                  <FingerprintPattern />
                  <span className="truncate">登录与 Passkey</span>
                </SidebarMenuButton>
              </SidebarMenuItem>
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>

      <SidebarFooter className="gap-0.5 border-t border-sidebar-border p-2.5 group-data-[collapsible=icon]:p-2">
        {retention ? (
          <div
            className="flex h-8 items-center gap-2 rounded-md px-2.5 text-xs text-muted-foreground group-data-[collapsible=icon]:hidden"
            title={`保留 ${retention.retention_days} 天 · 上限 ${formatBytes(maxBytes)}`}
          >
            <span className={cn("size-1.5 shrink-0 rounded-full", storageTone)} aria-hidden="true" />
            <HardDrive className="size-3.5 shrink-0 text-subtle-foreground" aria-hidden="true" />
            <span className="truncate tabular-nums">
              {formatBytes(usedBytes)} / {formatBytes(maxBytes)} ({retention.retention_days}d)
            </span>
          </div>
        ) : null}

        <button
          type="button"
          onClick={toggleSidebar}
          className="hidden h-8 items-center gap-2 rounded-md px-2.5 text-[0.8125rem] text-muted-foreground outline-none hover:bg-sidebar-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring lg:flex group-data-[collapsible=icon]:size-8 group-data-[collapsible=icon]:justify-center group-data-[collapsible=icon]:px-0"
          aria-label={collapseLabel}
          aria-expanded={open}
          title={`${collapseLabel} (${isMac ? "⌘B" : "Ctrl+B"})`}
        >
          <PanelLeftIcon className="size-4 shrink-0" />
          <span className="group-data-[collapsible=icon]:hidden">收起侧边栏</span>
        </button>
      </SidebarFooter>
    </Sidebar>
  );
}
