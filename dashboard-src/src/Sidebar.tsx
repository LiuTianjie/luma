import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarTrigger,
  useSidebar,
  SidebarMenu,
  SidebarMenuBadge,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarMenuSub,
  SidebarMenuSubButton,
  SidebarMenuSubItem,
} from "@/components/ui/sidebar";
import { DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuLabel, DropdownMenuRadioGroup, DropdownMenuRadioItem, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { Separator } from "@/components/ui/separator";
import { LogOut, Settings2, Monitor, Moon, Sun } from "lucide-react";
import type { ThemeMode } from "./useTheme";
import { buildNavGroups, type NavGroup } from "./navItems";
import type { DashboardViewModel, NavPage } from "./dashboardViewModel";
import { ROUTE_BY_PAGE } from "./routes";
import { toHref, useRouter } from "./router";
import type { Lang } from "./types";
import { t } from "./i18n";
import lumaLogoMark from "./assets/luma-logo-mark.png";

export function AppSidebar({
  lang,
  clusterId,
  vm,
  showFleetSummary = true,
  activeNavPage,
  onNavigate,
  onPrefetch,
  onSignOut,
  themeMode,
  onThemeModeChange,
  onLangChange,
}: {
  lang: Lang;
  clusterId: string;
  vm: DashboardViewModel;
  showFleetSummary?: boolean;
  activeNavPage: NavPage;
  onNavigate: (page: NavPage) => void;
  onPrefetch?: (page: NavPage) => void;
  onSignOut: () => void;
  themeMode: ThemeMode;
  onThemeModeChange: (mode: ThemeMode) => void;
  onLangChange: (lang: Lang) => void;
}) {
  const { open, openMobile, isMobile, setOpenMobile } = useSidebar();
  const expanded = isMobile ? openMobile : open;
  const toggleLabel = lang === "zh" ? (expanded ? "收起侧边栏" : "展开侧边栏") : (expanded ? "Collapse sidebar" : "Expand sidebar");
  const groups: NavGroup[] = buildNavGroups(lang, vm);
  const { path, navigate: navigatePath } = useRouter();
  const activeWorkspace = ["builder", "deploy"].includes(activeNavPage) ? "deployments"
    : ["storage", "registry"].includes(activeNavPage) ? "nodes" : activeNavPage;

  return (
    <Sidebar collapsible="icon" variant="sidebar" >
      <SidebarHeader className="h-(--console-header-height) justify-center">
        <div className="flex min-w-0 items-center gap-2 group-data-[collapsible=icon]:justify-center">
          <img src={lumaLogoMark} alt="" className="size-6 shrink-0 group-data-[collapsible=icon]:hidden" />
          <div className="flex min-w-0 flex-1 items-baseline gap-2 group-data-[collapsible=icon]:hidden"><strong className="text-lg font-semibold">Luma</strong><span className="truncate text-xs text-muted-foreground">{t(lang, "title")}</span></div>
          <SidebarTrigger  aria-label={toggleLabel} title={toggleLabel} aria-expanded={expanded} />
        </div>
      </SidebarHeader>
      <SidebarContent>
        <div className="flex min-w-0 items-center gap-2 px-4 py-3 text-xs group-data-[collapsible=icon]:hidden">
          <span className="shrink-0 text-muted-foreground">{t(lang, "cluster")}</span><code className="truncate" title={clusterId} translate="no">{clusterId}</code>
        </div>
        {groups.map((group) => (
          <SidebarGroup key={group.key}>
            {group.label ? <SidebarGroupLabel>{group.label}</SidebarGroupLabel> : null}
            <SidebarGroupContent>
              <SidebarMenu>
                {group.items.map((item) => {
                  const Icon = item.icon;
                  const showValue = typeof item.value === "number";
                  const active = activeWorkspace === item.id;
                  const tip = `${item.label} - ${item.detail}`;
                  return (
                    <SidebarMenuItem key={item.id}>
                      <SidebarMenuButton
                        isActive={active}
                        aria-label={item.label}
                        tooltip={tip}
                        render={
                          <a
                            href={toHref(ROUTE_BY_PAGE[item.id])}
                            aria-current={active ? "page" : undefined}
                            onClick={(event) => {
                              if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
                              event.preventDefault();
                              onNavigate(item.id);
                              if (isMobile) setOpenMobile(false);
                            }}
                            onPointerEnter={() => onPrefetch?.(item.id)}
                            onFocus={() => onPrefetch?.(item.id)}
                          />
                        }
                      >
                        <Icon />
                        <span className="truncate group-data-[collapsible=icon]:hidden">{item.label}</span>
                      </SidebarMenuButton>
                      {showValue ? <SidebarMenuBadge className="group-data-[collapsible=icon]:hidden">{item.value}</SidebarMenuBadge> : null}
                      {item.children?.length && active ? (
                        <SidebarMenuSub>
                          {item.children.map((child) => {
                            const activeChild = child.href === "/fleet"
                              ? path === child.href
                              : path === child.href || path.startsWith(`${child.href}/`);
                            return (
                              <SidebarMenuSubItem key={child.href}>
                                <SidebarMenuSubButton
                                  size="sm"
                                  isActive={activeChild}
                                  title={child.detail}
                                  render={<a href={toHref(child.href)} aria-current={activeChild ? "page" : undefined} />}
                                  onClick={(event) => {
                                    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
                                    event.preventDefault();
                                    navigatePath(child.href);
                                    if (isMobile) setOpenMobile(false);
                                  }}
                                >
                                  <span>{child.label}</span>
                                </SidebarMenuSubButton>
                              </SidebarMenuSubItem>
                            );
                          })}
                        </SidebarMenuSub>
                      ) : null}
                    </SidebarMenuItem>
                  );
                })}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
        ))}
      </SidebarContent>
      <SidebarFooter>
        <Separator />
        <SidebarMenu>
          <SidebarMenuItem>
            <DropdownMenu>
              <DropdownMenuTrigger render={<SidebarMenuButton tooltip={lang === "zh" ? "偏好" : "Preferences"} />}>
                <Settings2 data-icon="inline-start" aria-hidden="true" />
                <span>{lang === "zh" ? "偏好" : "Preferences"}</span>
              </DropdownMenuTrigger>
              <DropdownMenuContent side={isMobile || expanded ? "top" : "right"} align="start" className="min-w-56">
                <DropdownMenuGroup>
                  <DropdownMenuLabel>{lang === "zh" ? "外观" : "Appearance"}</DropdownMenuLabel>
                  <DropdownMenuRadioGroup value={themeMode} onValueChange={(value) => onThemeModeChange(value as ThemeMode)}>
                    {([["system", Monitor], ["light", Sun], ["dark", Moon]] as const).map(([mode, Icon]) => (
                      <DropdownMenuRadioItem key={mode} value={mode}>
                        <Icon aria-hidden="true" />
                        {mode === "system" ? (lang === "zh" ? "跟随系统" : "Follow system") : mode === "light" ? (lang === "zh" ? "日间模式" : "Light mode") : (lang === "zh" ? "夜间模式" : "Dark mode")}
                      </DropdownMenuRadioItem>
                    ))}
                  </DropdownMenuRadioGroup>
                </DropdownMenuGroup>
                <DropdownMenuSeparator />
                <DropdownMenuGroup>
                  <DropdownMenuLabel>{lang === "zh" ? "语言" : "Language"}</DropdownMenuLabel>
                  <DropdownMenuRadioGroup value={lang} onValueChange={(value) => onLangChange(value as Lang)}>
                    <DropdownMenuRadioItem value="zh">中文</DropdownMenuRadioItem>
                    <DropdownMenuRadioItem value="en">English</DropdownMenuRadioItem>
                  </DropdownMenuRadioGroup>
                </DropdownMenuGroup>
              </DropdownMenuContent>
            </DropdownMenu>
          </SidebarMenuItem>
          <SidebarMenuItem>
            <SidebarMenuButton onClick={onSignOut} tooltip={t(lang, "signOut")}>
              <LogOut data-icon="inline-start" aria-hidden="true" /><span>{t(lang, "signOut")}</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
        {showFleetSummary && <div className="flex flex-col gap-1 px-2 py-2 text-xs group-data-[collapsible=icon]:hidden">
          <p className="text-muted-foreground">{lang === "zh" ? "就绪节点" : "Ready nodes"} <span className="tabular-nums text-foreground">{vm.activeNodes} / {vm.nodes.length}</span></p>
          <p className="text-muted-foreground">{lang === "zh" ? `${Math.max(0, vm.services.length - vm.healthyServices)} 个服务异常` : `${Math.max(0, vm.services.length - vm.healthyServices)} services need attention`}</p>
        </div>}
      </SidebarFooter>
    </Sidebar>
  );
}
