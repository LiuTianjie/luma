import { parseObjectRoute } from "../objectRoutes";
import { parseApplicationPath, applicationPath } from "./applicationRoutes";
import { ArrowLeft, BookOpen, Check, Contrast, Languages, LogOut, Monitor, Moon, Sun, UserCircle } from "@/components/icons";
import { Button } from "@/components/ui/button";
import { Breadcrumb, BreadcrumbList, BreadcrumbItem, BreadcrumbLink, BreadcrumbPage, BreadcrumbSeparator } from "@/components/ui/breadcrumb";
import { DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { activeNavChild, buildNavGroups, navWorkspace } from "../navItems";
import { ROUTE_BY_PAGE } from "../routes";
import { useRouter, spaLink } from "../router";
import type { DashboardViewModel, NavPage } from "../dashboardViewModel";
import { SidebarTrigger } from "@/components/ui/sidebar";
import type { ThemeMode } from "../useTheme";
import { t } from "../i18n";
import type { Lang, SyncStatus } from "../types";

const DOCS_BASE = "https://liutianjie.github.io/luma/docs/";

type Props = {
  vm: DashboardViewModel;
  clusterId: string;
  activeNavPage: NavPage | null;
  lang: Lang;
  lastUpdated: Date | null;
  syncStatus: SyncStatus;
  themeMode: ThemeMode;
  onThemeModeChange: (mode: ThemeMode) => void;
  onLangChange: (lang: Lang) => void;
  onSignOut: () => void;
};

export function Topbar({
  vm,
  clusterId,
  activeNavPage,
  lang,
  lastUpdated,
  syncStatus,
  themeMode,
  onThemeModeChange,
  onLangChange,
  onSignOut,
}: Props) {
  const zh = lang === "zh";
  const { path, navigate } = useRouter();
  const applicationRoute = parseApplicationPath(path);
  // Node and service detail pages name the object as the last crumb.
  const objectRoute = parseObjectRoute(path);
  const objectName = objectRoute?.kind === "node" || objectRoute?.kind === "service" ? objectRoute.name : "";
  const item = activeNavPage ? buildNavGroups(lang, vm).flatMap(group => group.items).find(entry => entry.id === navWorkspace(activeNavPage)) : undefined;
  const child = activeNavChild(item, path);
  // Task pages without a sidebar entry still name themselves under their owning workspace.
  const pageTitle = activeNavPage === "deploy" ? (zh ? "创建应用" : "Create application")
    : activeNavPage === "builder" ? (zh ? "从 Git 构建" : "Build from Git") : "";
  const leaf = child?.label || pageTitle;
  const title = leaf || item?.label || (activeNavPage ? (zh ? "控制台" : "Console") : (zh ? "页面不存在" : "Page not found"));
  const timeFormatter = new Intl.DateTimeFormat(zh ? "zh-CN" : "en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  // Pages that load their own data have no snapshot time; say nothing rather than a bare "Updated".
  const statusText = syncStatus === "updated"
    ? lastUpdated ? `${t(lang, "updated")} ${timeFormatter.format(lastUpdated)}` : ""
    : t(lang, syncStatus);
  const statusTone = syncStatus === "updated" ? "bg-success" : syncStatus === "refreshing" ? "bg-info animate-pulse" : syncStatus === "notConnected" ? "bg-subtle-foreground" : "bg-destructive";
  const themeOptions = [
    { mode: "system" as const, icon: Monitor, label: zh ? "跟随系统" : "System" },
    { mode: "light" as const, icon: Sun, label: zh ? "浅色" : "Light" },
    { mode: "dark" as const, icon: Moon, label: zh ? "深色" : "Dark" },
  ];

  return (
    <header className="flex h-(--console-header-height) shrink-0 items-center gap-3 border-b border-border bg-background px-4 md:px-6">
      <SidebarTrigger className="-ml-1 lg:hidden" aria-label={zh ? "打开导航" : "Open navigation"} />
      {path.startsWith("/create/") && <Button variant="ghost" size="sm" onClick={() => navigate("/create")} aria-label={zh ? "返回创建" : "Back to create"}><ArrowLeft data-icon="inline-start" />{zh ? "返回" : "Back"}</Button>}
      <Breadcrumb className="min-w-0" aria-label={zh ? "当前位置" : "Current location"}>
        <BreadcrumbList className="flex-nowrap text-[0.8125rem]">
          {applicationRoute.stack ? <>
            <BreadcrumbItem><BreadcrumbLink {...spaLink("/apps", navigate)}>{zh ? "应用" : "Applications"}</BreadcrumbLink></BreadcrumbItem>
            <BreadcrumbSeparator />
            <BreadcrumbItem className="min-w-0">{applicationRoute.service ? <BreadcrumbLink {...spaLink(applicationPath(applicationRoute.stack), navigate)} className="truncate" title={applicationRoute.stack}>{applicationRoute.stack}</BreadcrumbLink> : <BreadcrumbPage className="truncate font-medium" title={applicationRoute.stack}>{applicationRoute.stack}</BreadcrumbPage>}</BreadcrumbItem>
            {applicationRoute.service && <><BreadcrumbSeparator /><BreadcrumbItem className="min-w-0"><BreadcrumbPage className="truncate font-medium" title={applicationRoute.service}>{applicationRoute.service}</BreadcrumbPage></BreadcrumbItem></>}
          </> : <>
            {(leaf || objectName) && item ? <><BreadcrumbItem className="hidden sm:inline-flex"><BreadcrumbLink {...spaLink(ROUTE_BY_PAGE[item.id], navigate)}>{item.label}</BreadcrumbLink></BreadcrumbItem><BreadcrumbSeparator className="hidden sm:block" /></> : null}
            {objectName ? <>
              {child ? <><BreadcrumbItem><BreadcrumbLink {...spaLink(child.href, navigate)}>{child.label}</BreadcrumbLink></BreadcrumbItem><BreadcrumbSeparator /></> : null}
              <BreadcrumbItem className="min-w-0"><BreadcrumbPage className="truncate font-medium" title={objectName}>{objectName}</BreadcrumbPage></BreadcrumbItem>
            </> : <BreadcrumbItem><BreadcrumbPage className="font-medium">{title}</BreadcrumbPage></BreadcrumbItem>}
          </>}
        </BreadcrumbList>
      </Breadcrumb>

      <div className="ml-auto flex items-center gap-1">
        {statusText ? (
          <span className="mr-2 hidden items-center gap-1.5 text-xs text-muted-foreground tabular-nums sm:inline-flex" role="status" title={statusText}>
            <span className={cn("size-1.5 rounded-full", statusTone)} aria-hidden="true" />
            {statusText}
          </span>
        ) : null}
        <Tooltip>
          <TooltipTrigger render={<Button variant="ghost" size="icon-sm" nativeButton={false} render={<a href={`${DOCS_BASE}${zh ? "dashboard-guide.zh-CN.html" : "dashboard-guide.html"}`} target="_blank" rel="noreferrer" />} aria-label={zh ? "控制台文档（新窗口）" : "Console docs (new window)"} />}>
            <BookOpen />
          </TooltipTrigger>
          <TooltipContent>{zh ? "文档" : "Docs"}</TooltipContent>
        </Tooltip>
        <DropdownMenu>
          <DropdownMenuTrigger render={<Button variant="ghost" size="icon-sm" aria-label={zh ? "账户与偏好" : "Account and preferences"} />}>
            <UserCircle className="size-[18px]" />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" sideOffset={6} className="w-60">
            <DropdownMenuGroup>
              <DropdownMenuLabel className="flex flex-col gap-0.5 py-2">
                <span className="text-[0.8125rem] font-medium text-foreground">{zh ? "管理员" : "Administrator"}</span>
                {clusterId ? <span className="truncate font-mono text-[0.6875rem] font-normal text-muted-foreground" translate="no">{clusterId}</span> : null}
              </DropdownMenuLabel>
            </DropdownMenuGroup>
            <DropdownMenuSeparator />
            <DropdownMenuGroup>
              <DropdownMenuLabel className="flex items-center gap-2"><Contrast className="size-3.5" />{zh ? "外观" : "Appearance"}</DropdownMenuLabel>
              <div className="grid grid-cols-3 gap-1 px-1.5 pb-1.5" role="radiogroup" aria-label={zh ? "外观" : "Appearance"}>
                {themeOptions.map(({ mode, icon: Icon, label }) => (
                  <button
                    key={mode}
                    type="button"
                    role="radio"
                    aria-checked={themeMode === mode}
                    onClick={() => onThemeModeChange(mode)}
                    className={cn("flex h-14 flex-col items-center justify-center gap-1 rounded-md text-[0.6875rem] text-muted-foreground ring-1 ring-border outline-none transition-colors hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring", themeMode === mode && "bg-accent text-foreground ring-foreground/30")}
                  >
                    <Icon className="size-4" />
                    {label}
                  </button>
                ))}
              </div>
            </DropdownMenuGroup>
            <DropdownMenuSeparator />
            <DropdownMenuGroup>
              <DropdownMenuLabel className="flex items-center gap-2"><Languages className="size-3.5" />{zh ? "语言" : "Language"}</DropdownMenuLabel>
              {([["zh", "简体中文"], ["en", "English"]] as const).map(([value, label]) => (
                <DropdownMenuItem key={value} onClick={() => onLangChange(value)} aria-checked={lang === value} role="menuitemradio">
                  {label}
                  {lang === value ? <Check className="ml-auto size-3.5" /> : null}
                </DropdownMenuItem>
              ))}
            </DropdownMenuGroup>
            <DropdownMenuSeparator />
            <DropdownMenuItem onClick={onSignOut}>
              <LogOut />
              {t(lang, "signOut")}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </header>
  );
}
