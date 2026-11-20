import { useEffect } from "react";
import { Link, useLocation, useNavigate } from "react-router";
import {
  Contrast,
  FingerprintPattern,
  LogOut,
  Monitor,
  Moon,
  RefreshCw,
  Sun,
  UserCircle,
} from "@/components/icons";
import type { AuthStatus, Project } from "@/api/types";
import { SelectControl } from "@/components/primitives";
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { SidebarTrigger } from "@/components/ui/sidebar";
import type { ThemeMode } from "@/hooks/useTheme";
import { cn } from "@/lib/utils";
import { paths, type ProjectRouteSegment } from "@/router/paths";
import { shortId } from "@/utils/format";

const SECTION_LABELS: Record<string, string> = {
  overview: "流量与地域概览",
  sessions: "用户会话与轨迹",
  users: "用户全量轨迹",
  events: "实时事件流",
  issues: "异常与黑匣子",
  performance: "Web Vitals 性能",
  funnel: "转化漏斗与留存",
  logs: "客户端日志",
  settings: "接入与项目设置",
};

const TIME_WINDOWS = [
  { value: "1h", label: "近 1 小时" },
  { value: "24h", label: "近 24 小时" },
  { value: "7d", label: "近 7 天" },
];

export function Topbar({
  projects,
  authStatus,
  since,
  onChangeSince,
  autoRefresh,
  onToggleAutoRefresh,
  onRefresh,
  themeMode,
  onThemeModeChange,
  onSignOut,
}: {
  projects: Project[];
  authStatus: AuthStatus | null;
  since: string;
  onChangeSince: (next: string) => void;
  autoRefresh: boolean;
  onToggleAutoRefresh: () => void;
  onRefresh: () => void;
  themeMode: ThemeMode;
  onThemeModeChange: (mode: ThemeMode) => void;
  onSignOut: () => void;
}) {
  const location = useLocation();
  const navigate = useNavigate();

  const segs = location.pathname.split("/").filter(Boolean);
  const isAccount = segs[0] === "account";
  const isProject = segs[0] === "projects" && Boolean(segs[1]);
  const projectSlug = isProject ? decodeURIComponent(segs[1]) : "";
  const sectionKey = isProject && segs[2] ? segs[2] : "overview";
  const detailParam = isProject && segs[3] ? decodeURIComponent(segs[3]) : "";

  const currentProject = projects.find((p) => p.slug === projectSlug);
  const projectTitle = currentProject?.name || projectSlug;
  const sectionTitle = SECTION_LABELS[sectionKey] || sectionKey;

  useEffect(() => {
    if (isAccount) {
      document.title = "登录与 Passkey · Luma Eye";
      return;
    }
    if (!isProject) {
      document.title = "多项目总览 · Luma Eye";
      return;
    }
    if (detailParam) {
      document.title = `${shortId(detailParam, 14)} · ${sectionTitle} · ${projectTitle} · Luma Eye`;
      return;
    }
    document.title = `${sectionTitle} · ${projectTitle} · Luma Eye`;
  }, [isAccount, isProject, projectTitle, sectionTitle, detailParam]);

  const themeOptions = [
    { mode: "system" as const, icon: Monitor, label: "跟随系统" },
    { mode: "light" as const, icon: Sun, label: "浅色" },
    { mode: "dark" as const, icon: Moon, label: "深色" },
  ];

  const parentSectionSegment: ProjectRouteSegment =
    sectionKey === "users" ? "sessions" : (sectionKey as ProjectRouteSegment);

  return (
    <header className="flex h-(--console-header-height) shrink-0 items-center gap-3 border-b border-border bg-background px-4 md:px-6">
      <SidebarTrigger className="-ml-1 lg:hidden" aria-label="打开导航" />

      <Breadcrumb className="min-w-0" aria-label="当前位置">
        <BreadcrumbList className="flex-nowrap text-[0.8125rem]">
          {isAccount ? (
            <>
              <BreadcrumbItem className="hidden sm:inline-flex">
                <BreadcrumbLink render={<Link to={paths.portfolio(since)} />}>控制台</BreadcrumbLink>
              </BreadcrumbItem>
              <BreadcrumbSeparator className="hidden sm:block" />
              <BreadcrumbItem>
                <BreadcrumbPage className="font-medium">登录与 Passkey</BreadcrumbPage>
              </BreadcrumbItem>
            </>
          ) : isProject ? (
            <>
              <BreadcrumbItem className="hidden sm:inline-flex">
                <BreadcrumbLink render={<Link to={paths.portfolio(since)} />}>多项目总览</BreadcrumbLink>
              </BreadcrumbItem>
              <BreadcrumbSeparator className="hidden sm:block" />
              <BreadcrumbItem className="min-w-0">
                <BreadcrumbLink
                  render={<Link to={paths.projectSection(projectSlug, "overview", since)} />}
                  className="truncate"
                >
                  {projectTitle}
                </BreadcrumbLink>
              </BreadcrumbItem>
              <BreadcrumbSeparator />
              {detailParam ? (
                <>
                  <BreadcrumbItem className="hidden md:inline-flex">
                    <BreadcrumbLink
                      render={<Link to={paths.projectSection(projectSlug, parentSectionSegment, since)} />}
                    >
                      {sectionTitle}
                    </BreadcrumbLink>
                  </BreadcrumbItem>
                  <BreadcrumbSeparator className="hidden md:block" />
                  <BreadcrumbItem className="min-w-0">
                    <BreadcrumbPage className="truncate font-mono font-medium" title={detailParam}>
                      {shortId(detailParam, 18)}
                    </BreadcrumbPage>
                  </BreadcrumbItem>
                </>
              ) : (
                <BreadcrumbItem className="min-w-0">
                  <BreadcrumbPage className="truncate font-medium">{sectionTitle}</BreadcrumbPage>
                </BreadcrumbItem>
              )}
            </>
          ) : (
            <BreadcrumbItem>
              <BreadcrumbPage className="font-medium">多项目总览</BreadcrumbPage>
            </BreadcrumbItem>
          )}
        </BreadcrumbList>
      </Breadcrumb>

      <div className="ml-auto flex items-center gap-1.5">
        {!isAccount ? (
          <>
            <SelectControl
              ariaLabel="时间范围"
              value={since}
              options={TIME_WINDOWS}
              onChange={onChangeSince}
              className="w-28"
            />
            <Button
              type="button"
              variant={autoRefresh ? "secondary" : "outline"}
              size="sm"
              aria-pressed={autoRefresh}
              onClick={onToggleAutoRefresh}
              className="hidden sm:inline-flex"
            >
              <span
                className={cn(
                  "size-1.5 rounded-full",
                  autoRefresh ? "bg-success animate-pulse" : "bg-subtle-foreground",
                )}
                aria-hidden="true"
              />
              <span>15s 轮询</span>
            </Button>
            <Button
              type="button"
              variant="outline"
              size="icon-sm"
              onClick={onRefresh}
              title="立即刷新"
              aria-label="立即刷新"
            >
              <RefreshCw className="size-3.5" />
            </Button>
          </>
        ) : null}

        <DropdownMenu>
          <DropdownMenuTrigger
            render={<Button variant="ghost" size="icon-sm" aria-label="账户与外观设置" />}
          >
            <UserCircle className="size-[18px]" />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" sideOffset={6} className="w-60">
            <DropdownMenuGroup>
              <DropdownMenuLabel className="flex flex-col gap-0.5 py-2">
                <span className="text-[0.8125rem] font-medium text-foreground">
                  {authStatus?.display_name || authStatus?.username || "Administrator"}
                </span>
                <span className="truncate font-mono text-[0.6875rem] font-normal text-muted-foreground">
                  {authStatus?.username || "admin"} · 30d 持久会话
                </span>
              </DropdownMenuLabel>
            </DropdownMenuGroup>
            <DropdownMenuSeparator />
            <DropdownMenuGroup>
              <DropdownMenuLabel className="flex items-center gap-2">
                <Contrast className="size-3.5" />
                外观主题
              </DropdownMenuLabel>
              <div className="grid grid-cols-3 gap-1 px-1.5 pb-1.5" role="radiogroup" aria-label="外观主题">
                {themeOptions.map(({ mode, icon: Icon, label }) => (
                  <button
                    key={mode}
                    type="button"
                    role="radio"
                    aria-checked={themeMode === mode}
                    onClick={() => onThemeModeChange(mode)}
                    className={cn(
                      "flex h-13 flex-col items-center justify-center gap-1 rounded-md text-[0.6875rem] text-muted-foreground ring-1 ring-border transition-colors outline-none hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring",
                      themeMode === mode && "bg-accent text-foreground ring-foreground/30",
                    )}
                  >
                    <Icon className="size-4" />
                    {label}
                  </button>
                ))}
              </div>
            </DropdownMenuGroup>
            <DropdownMenuSeparator />
            <DropdownMenuItem onClick={() => navigate(paths.account())}>
              <FingerprintPattern />
              登录密码与 Passkey
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem onClick={onSignOut}>
              <LogOut />
              退出登录
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </header>
  );
}
