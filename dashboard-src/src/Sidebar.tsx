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
  SidebarMenuSub,
  SidebarMenuSubButton,
  SidebarMenuSubItem,
  SidebarTrigger,
} from "@/components/ui/sidebar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { LogOut, RefreshCw } from "lucide-react";
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
  activeNavPage,
  onNavigate,
  onPrefetch,
  onRefresh,
  onSignOut,
}: {
  lang: Lang;
  clusterId: string;
  vm: DashboardViewModel;
  activeNavPage: NavPage;
  onNavigate: (page: NavPage) => void;
  onPrefetch?: (page: NavPage) => void;
  onRefresh: () => void;
  onSignOut: () => void;
}) {
  const groups: NavGroup[] = buildNavGroups(lang, vm);
  const { path, navigate: navigatePath } = useRouter();
  const activeWorkspace = ["builder", "deploy"].includes(activeNavPage) ? "deployments"
    : ["storage", "registry"].includes(activeNavPage) ? "nodes" : activeNavPage;

  return (
    <Sidebar collapsible="icon" variant="sidebar">
      <SidebarHeader>
        <div className="flex items-center gap-2 px-2 py-2 group-data-[collapsible=icon]:flex-col group-data-[collapsible=icon]:px-1">
          <div className="flex size-8 items-center justify-center overflow-hidden rounded-lg bg-sidebar-accent">
            <img src={lumaLogoMark} alt="" className="size-5" />
          </div>
          <div className="flex min-w-0 flex-1 flex-col group-data-[collapsible=icon]:hidden">
            <span className="truncate text-xs text-muted-foreground">Luma</span>
            <strong className="truncate text-sm font-medium">{t(lang, "title")}</strong>
          </div>
          <SidebarTrigger className="ml-auto shrink-0 group-data-[collapsible=icon]:ml-0" />
        </div>
        <div className="flex min-w-0 items-center gap-2 px-2 pb-2 group-data-[collapsible=icon]:hidden">
          <span className="text-xs text-muted-foreground">{t(lang, "cluster")}</span>
          <Badge variant="outline" className="min-w-0 max-w-full truncate font-mono" translate="no">{clusterId}</Badge>
        </div>
      </SidebarHeader>
      <SidebarContent>
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
          <Button variant="ghost" size="sm" className="flex-1 justify-start group-data-[collapsible=icon]:size-8 group-data-[collapsible=icon]:flex-none group-data-[collapsible=icon]:justify-center" onClick={onRefresh} title={t(lang, "refresh")}><RefreshCw data-icon="inline-start" /><span className="group-data-[collapsible=icon]:hidden">{t(lang, "refresh")}</span></Button>
          <Button variant="ghost" size="sm" className="flex-1 justify-start group-data-[collapsible=icon]:size-8 group-data-[collapsible=icon]:flex-none group-data-[collapsible=icon]:justify-center" onClick={onSignOut} title={t(lang, "signOut")}><LogOut data-icon="inline-start" /><span className="group-data-[collapsible=icon]:hidden">{t(lang, "signOut")}</span></Button>
        </div>
        <div className="flex flex-col gap-1.5 rounded-lg bg-sidebar-accent px-3 py-3 text-xs group-data-[collapsible=icon]:hidden">
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
        </div>
      </SidebarFooter>
    </Sidebar>
  );
}
