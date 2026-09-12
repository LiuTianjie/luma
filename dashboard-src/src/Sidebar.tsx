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
import { Button } from "@/components/ui/button";
import { LogOut, Settings2, Monitor, Moon, Sun } from "lucide-react";
import { useState } from "react";
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
  const { open, openMobile, isMobile } = useSidebar();
  const expanded = isMobile ? openMobile : open;
  const toggleLabel = lang === "zh" ? (expanded ? "收起侧边栏" : "展开侧边栏") : (expanded ? "Collapse sidebar" : "Expand sidebar");
  const groups: NavGroup[] = buildNavGroups(lang, vm);
  const { path, navigate: navigatePath } = useRouter();
  const activeWorkspace = ["builder", "deploy"].includes(activeNavPage) ? "deployments"
    : ["storage", "registry"].includes(activeNavPage) ? "nodes" : activeNavPage;
  const [preferencesOpen, setPreferencesOpen] = useState(false);

  return (
    <Sidebar collapsible="icon" variant="sidebar" className="console-sidebar">
      <SidebarHeader>
        <div className="console-brand">
          <img src={lumaLogoMark} alt="" />
          <div className="console-brand-copy"><strong>Luma</strong><span>{t(lang, "title")}</span></div>
          <SidebarTrigger className="console-sidebar-toggle" aria-label={toggleLabel} title={toggleLabel} aria-expanded={expanded} />
        </div>
      </SidebarHeader>
      <SidebarContent>
        <div className="console-cluster">
          <span>{t(lang, "cluster")}</span><code translate="no">{clusterId}</code>
        </div>
        {groups.map((group) => (
          <SidebarGroup key={group.key} className="p-3">
            {group.label ? <SidebarGroupLabel className="h-8 px-2">{group.label}</SidebarGroupLabel> : null}
            <SidebarGroupContent>
              <SidebarMenu className="gap-2">
                {group.items.map((item) => {
                  const Icon = item.icon;
                  const showValue = typeof item.value === "number";
                  const active = activeWorkspace === item.id;
                  const tip = `${item.label} - ${item.detail}`;
                  return (
                    <SidebarMenuItem key={item.id}>
                      <SidebarMenuButton
                        isActive={active}
                        className="h-9 group-data-[collapsible=icon]:size-8"
                        tooltip={tip}
                        render={
                          <a
                            href={toHref(ROUTE_BY_PAGE[item.id])}
                            aria-current={active ? "page" : undefined}
                            onClick={(event) => {
                              if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
                              event.preventDefault();
                              onNavigate(item.id);
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
        <div className="flex items-center gap-1 px-2 group-data-[collapsible=icon]:flex-col">
          <div className="relative flex-1 group-data-[collapsible=icon]:flex-none">
            <Button variant="ghost" size="sm" className="w-full justify-start group-data-[collapsible=icon]:size-8 group-data-[collapsible=icon]:justify-center" title={lang === "zh" ? "偏好" : "Preferences"} onClick={() => setPreferencesOpen((open) => !open)} aria-expanded={preferencesOpen}><Settings2 data-icon="inline-start" /><span className="group-data-[collapsible=icon]:hidden">{lang === "zh" ? "偏好" : "Preferences"}</span></Button>
            {preferencesOpen ? <div className="absolute bottom-full left-0 z-50 mb-2 w-56 rounded-lg border bg-popover p-2 text-sm text-popover-foreground shadow-md group-data-[collapsible=icon]:left-10">
              <p className="px-2 py-1 text-xs font-medium text-muted-foreground">{lang === "zh" ? "外观" : "Appearance"}</p>
              {([["system", Monitor], ["light", Sun], ["dark", Moon]] as const).map(([mode, Icon]) => <button type="button" key={mode} className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left hover:bg-accent" aria-pressed={themeMode === mode} onClick={() => { onThemeModeChange(mode); setPreferencesOpen(false); }}><Icon className="size-4" />{mode === "system" ? (lang === "zh" ? "跟随系统" : "Follow system") : mode === "light" ? (lang === "zh" ? "日间模式" : "Light mode") : (lang === "zh" ? "夜间模式" : "Dark mode")}</button>)}
              <div className="my-2 border-t" />
              <p className="px-2 py-1 text-xs font-medium text-muted-foreground">{lang === "zh" ? "语言" : "Language"}</p>
              {([["zh", "中文"], ["en", "English"]] as const).map(([value, label]) => <button type="button" key={value} className="flex w-full items-center rounded-md px-2 py-1.5 text-left hover:bg-accent" aria-pressed={lang === value} onClick={() => { onLangChange(value); setPreferencesOpen(false); }}>{label}</button>)}
            </div> : null}
          </div>
          <Button variant="ghost" size="sm" className="flex-1 justify-start group-data-[collapsible=icon]:size-8 group-data-[collapsible=icon]:flex-none group-data-[collapsible=icon]:justify-center" onClick={onSignOut} title={t(lang, "signOut")}><LogOut data-icon="inline-start" /><span className="group-data-[collapsible=icon]:hidden">{t(lang, "signOut")}</span></Button>
        </div>
        {showFleetSummary && <div className="flex flex-col gap-1.5 rounded-lg bg-sidebar-accent px-3 py-3 text-xs group-data-[collapsible=icon]:hidden">
          <span className="text-muted-foreground">{lang === "zh" ? "就绪节点" : "Ready nodes"}</span>
          <strong className="text-sm font-medium">
            {vm.activeNodes}
            <small className="text-muted-foreground"> / {vm.nodes.length}</small>
          </strong>
          <span className="text-muted-foreground">
            {lang === "zh"
              ? `${Math.max(0, vm.services.length - vm.healthyServices)} 个服务异常`
              : `${Math.max(0, vm.services.length - vm.healthyServices)} services need attention`}
          </span>
        </div>}
      </SidebarFooter>
    </Sidebar>
  );
}
