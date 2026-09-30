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
import { DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuLabel, DropdownMenuLinkItem, DropdownMenuRadioGroup, DropdownMenuRadioItem, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { Separator } from "@/components/ui/separator";
import { LogOut, Settings2, Monitor, Moon, Sun } from "lucide-react";
import type { ThemeMode } from "./useTheme";
import { activeNavChild, buildNavGroups, navWorkspace, type NavGroup } from "./navItems";
import type { DashboardViewModel, NavPage } from "./dashboardViewModel";
import { ROUTE_BY_PAGE } from "./routes";
import { toHref, useRouter, isPlainLeftClick, spaLink } from "./router";
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
  activeNavPage: NavPage | null;
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
  const activeWorkspace = activeNavPage ? navWorkspace(activeNavPage) : null;
  // Icon-only mode hides nested lists, so workspaces with secondary pages open a flyout instead.
  const iconOnly = !isMobile && !open;
  const closeMobile = () => { if (isMobile) setOpenMobile(false); };

  return (
    <Sidebar collapsible="icon" variant="sidebar" >
      <SidebarHeader className="h-(--console-header-height) justify-center px-5 group-data-[collapsible=icon]:px-2">
        <div className="flex min-w-0 items-center gap-2 group-data-[collapsible=icon]:justify-center">
          <img src={lumaLogoMark} alt="" className="size-6 shrink-0 group-data-[collapsible=icon]:hidden" />
          <div className="flex min-w-0 flex-1 items-baseline gap-2 group-data-[collapsible=icon]:hidden"><strong className="text-lg font-semibold">Luma</strong><span className="truncate text-xs text-muted-foreground">{t(lang, "title")}</span></div>
          <SidebarTrigger  aria-label={toggleLabel} title={toggleLabel} aria-expanded={expanded} />
        </div>
      </SidebarHeader>
      <SidebarContent>
        {clusterId ? <div className="flex min-w-0 items-center gap-2 px-5 pt-4 text-xs group-data-[collapsible=icon]:hidden">
          <span className="shrink-0 text-muted-foreground">{t(lang, "cluster")}</span><code className="truncate" title={clusterId} translate="no">{clusterId}</code>
        </div> : null}
        {groups.map((group) => (
          <SidebarGroup key={group.key} className="px-3 pt-4 pb-2 group-data-[collapsible=icon]:px-2">
            {group.label ? <SidebarGroupLabel>{group.label}</SidebarGroupLabel> : null}
            <SidebarGroupContent>
              <SidebarMenu className="gap-1">
                {group.items.map((item) => {
                  const Icon = item.icon;
                  const showValue = typeof item.value === "number";
                  const active = activeWorkspace === item.id;
                  const tip = `${item.label} - ${item.detail}`;
                  const activeChild = active ? activeNavChild(item, path) : undefined;
                  if (iconOnly && item.children?.length) {
                    return (
                      <SidebarMenuItem key={item.id}>
                        <DropdownMenu>
                          <DropdownMenuTrigger
                            onPointerEnter={() => onPrefetch?.(item.id)}
                            onFocus={() => onPrefetch?.(item.id)}
                            render={<SidebarMenuButton isActive={active} className="h-10" aria-label={item.label} tooltip={tip} />}
                          >
                            <Icon />
                            <span className="truncate group-data-[collapsible=icon]:hidden">{item.label}</span>
                          </DropdownMenuTrigger>
                          <DropdownMenuContent side="right" align="start" sideOffset={8} className="min-w-48">
                            <DropdownMenuGroup>
                              <DropdownMenuLabel>{item.label}</DropdownMenuLabel>
                              {item.children.map((child) => (
                                <DropdownMenuLinkItem
                                  key={child.href}
                                  closeOnClick
                                  {...spaLink(child.href, navigatePath)}
                                  title={child.detail}
                                  aria-current={activeChild === child ? "page" : undefined}
                                  className="aria-[current=page]:font-medium aria-[current=page]:text-foreground"
                                >
                                  {child.label}
                                </DropdownMenuLinkItem>
                              ))}
                            </DropdownMenuGroup>
                          </DropdownMenuContent>
                        </DropdownMenu>
                        {showValue ? <SidebarMenuBadge className="group-data-[collapsible=icon]:hidden">{item.value}</SidebarMenuBadge> : null}
                      </SidebarMenuItem>
                    );
                  }
                  return (
                    <SidebarMenuItem key={item.id}>
                      <SidebarMenuButton
                        isActive={active}
                        className="h-10"
                        aria-label={item.label}
                        tooltip={tip}
                        render={
                          <a
                            href={toHref(ROUTE_BY_PAGE[item.id])}
                            aria-current={active && !activeChild ? "page" : undefined}
                            onClick={(event) => {
                              if (!isPlainLeftClick(event)) return;
                              event.preventDefault();
                              onNavigate(item.id);
                              closeMobile();
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
                        <SidebarMenuSub className="py-1">
                          {item.children.map((child) => (
                            <SidebarMenuSubItem key={child.href}>
                              <SidebarMenuSubButton
                                size="md"
                                className="h-9"
                                isActive={activeChild === child}
                                title={child.detail}
                                render={<a {...spaLink(child.href, navigatePath, closeMobile)} aria-current={activeChild === child ? "page" : undefined} />}
                              >
                                <span>{child.label}</span>
                              </SidebarMenuSubButton>
                            </SidebarMenuSubItem>
                          ))}
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
      <SidebarFooter className="gap-3 p-3 group-data-[collapsible=icon]:p-2">
        <Separator />
        <SidebarMenu className="gap-1">
          <SidebarMenuItem>
            <DropdownMenu>
              <DropdownMenuTrigger render={<SidebarMenuButton className="h-10" tooltip={lang === "zh" ? "偏好" : "Preferences"} />}>
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
            <SidebarMenuButton className="h-10" onClick={onSignOut} tooltip={t(lang, "signOut")}>
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
