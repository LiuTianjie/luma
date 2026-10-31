import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  useSidebar,
  SidebarMenu,
  SidebarMenuBadge,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarMenuSub,
  SidebarMenuSubButton,
  SidebarMenuSubItem,
} from "@/components/ui/sidebar";
import { DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuLabel, DropdownMenuLinkItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { ChevronDown, PanelLeftIcon, Search } from "@/components/icons";
import { cn } from "@/lib/utils";
import { activeNavChild, buildNavGroups, navWorkspace, type NavGroup } from "./navItems";
import { NAV_ICONS } from "./navIcons";
import type { DashboardViewModel, NavPage } from "./dashboardViewModel";
import { ROUTE_BY_PAGE } from "./routes";
import { toHref, useRouter, isPlainLeftClick, spaLink } from "./router";
import type { Lang } from "./types";
import { t } from "./i18n";
import lumaLogoMark from "./assets/luma-logo-mark.png";

export const isMac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);

export function AppSidebar({
  lang,
  clusterId,
  vm,
  showFleetSummary = true,
  activeNavPage,
  onNavigate,
  onPrefetch,
  onOpenPalette,
}: {
  lang: Lang;
  clusterId: string;
  vm: DashboardViewModel;
  showFleetSummary?: boolean;
  activeNavPage: NavPage | null;
  onNavigate: (page: NavPage) => void;
  onPrefetch?: (page: NavPage) => void;
  onOpenPalette: () => void;
}) {
  const zh = lang === "zh";
  const { open, isMobile, setOpenMobile, toggleSidebar } = useSidebar();
  const groups: NavGroup[] = buildNavGroups(lang, vm);
  const { path, navigate: navigatePath } = useRouter();
  const activeWorkspace = activeNavPage ? navWorkspace(activeNavPage) : null;
  // Icon-only mode hides nested lists, so workspaces with secondary pages open a flyout instead.
  const iconOnly = !isMobile && !open;
  const closeMobile = () => { if (isMobile) setOpenMobile(false); };
  const unhealthy = Math.max(0, vm.services.length - vm.healthyServices);
  const fleetTone = !vm.nodes.length ? "bg-subtle-foreground" : vm.activeNodes < vm.nodes.length || unhealthy ? "bg-warning" : "bg-success";
  const collapseLabel = open ? (zh ? "收起侧边栏" : "Collapse sidebar") : (zh ? "展开侧边栏" : "Expand sidebar");

  return (
    <Sidebar collapsible="icon" variant="sidebar">
      <SidebarHeader className="h-(--console-header-height) shrink-0 justify-center border-b border-sidebar-border px-3 group-data-[collapsible=icon]:px-2">
        <a {...spaLink("/", navigatePath, closeMobile)} className="flex min-w-0 items-center gap-2.5 rounded-md px-1 py-1 outline-none focus-visible:ring-2 focus-visible:ring-ring group-data-[collapsible=icon]:justify-center group-data-[collapsible=icon]:px-0" aria-label={zh ? "Luma 控制台首页" : "Luma console home"}>
          <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-background shadow-xs ring-1 ring-border">
            <img src={lumaLogoMark} alt="" className="size-5" />
          </span>
          <span className="flex min-w-0 flex-col gap-1 leading-none group-data-[collapsible=icon]:hidden">
            <strong className="text-[0.9375rem] font-semibold tracking-[-0.01em]">Luma</strong>
            {clusterId ? <span className="truncate font-mono text-[0.6875rem] text-muted-foreground" title={`${t(lang, "cluster")} ${clusterId}`} translate="no">{clusterId}</span> : <span className="text-[0.6875rem] text-muted-foreground">{t(lang, "title")}</span>}
          </span>
        </a>
      </SidebarHeader>

      <SidebarContent className="gap-0 px-2.5 pt-3 pb-2 group-data-[collapsible=icon]:px-2">
        <button
          type="button"
          onClick={onOpenPalette}
          className="mb-2 flex h-8 w-full shrink-0 items-center gap-2 rounded-lg bg-background px-2.5 text-[0.8125rem] text-muted-foreground shadow-xs ring-1 ring-border transition-colors outline-none hover:bg-accent/60 hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring group-data-[collapsible=icon]:size-8 group-data-[collapsible=icon]:justify-center group-data-[collapsible=icon]:px-0"
          aria-label={zh ? "搜索或跳转" : "Search or jump to"}
          aria-keyshortcuts={isMac ? "Meta+K" : "Control+K"}
        >
          <Search className="size-4 shrink-0" />
          <span className="flex-1 truncate text-left group-data-[collapsible=icon]:hidden">{zh ? "搜索或跳转…" : "Search or jump to…"}</span>
          <kbd className="rounded border border-border bg-muted px-1 font-sans text-[0.6875rem] leading-4 text-muted-foreground group-data-[collapsible=icon]:hidden">{isMac ? "⌘K" : "Ctrl K"}</kbd>
        </button>

        {groups.map((group) => (
          <SidebarGroup key={group.key} className="px-0 py-1.5">
            {group.label ? <SidebarGroupLabel className="h-7 px-2.5 text-[0.6875rem] font-medium tracking-wide text-subtle-foreground uppercase">{group.label}</SidebarGroupLabel> : null}
            <SidebarGroupContent>
              <SidebarMenu className="gap-px">
                {group.items.map((item) => {
                  const Icon = NAV_ICONS[item.icon];
                  const showValue = typeof item.value === "number";
                  const active = activeWorkspace === item.id;
                  const activeChild = active ? activeNavChild(item, path) : undefined;
                  const hasChildren = Boolean(item.children?.length);
                  if (iconOnly && hasChildren) {
                    return (
                      <SidebarMenuItem key={item.id}>
                        <DropdownMenu>
                          <DropdownMenuTrigger
                            onPointerEnter={() => onPrefetch?.(item.id)}
                            onFocus={() => onPrefetch?.(item.id)}
                            render={<SidebarMenuButton isActive={active} aria-label={item.label} tooltip={item.label} />}
                          >
                            <Icon />
                            <span className="truncate group-data-[collapsible=icon]:hidden">{item.label}</span>
                          </DropdownMenuTrigger>
                          <DropdownMenuContent side="right" align="start" sideOffset={10} className="min-w-52">
                            <DropdownMenuGroup>
                              <DropdownMenuLabel>{item.label}</DropdownMenuLabel>
                              {item.children!.map((child) => (
                                <DropdownMenuLinkItem
                                  key={child.href}
                                  closeOnClick
                                  {...spaLink(child.href, navigatePath)}
                                  title={child.detail}
                                  aria-current={activeChild === child ? "page" : undefined}
                                  className="aria-[current=page]:bg-accent aria-[current=page]:font-medium aria-[current=page]:text-foreground"
                                >
                                  {child.label}
                                </DropdownMenuLinkItem>
                              ))}
                            </DropdownMenuGroup>
                          </DropdownMenuContent>
                        </DropdownMenu>
                      </SidebarMenuItem>
                    );
                  }
                  return (
                    <SidebarMenuItem key={item.id}>
                      <SidebarMenuButton
                        // A workspace with children only highlights the child; the parent stays bold.
                        isActive={active && !hasChildren}
                        aria-label={item.label}
                        tooltip={item.label}
                        className={cn(hasChildren && active && "font-medium text-foreground [&>svg:first-child]:text-foreground")}
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
                        <span className="truncate">{item.label}</span>
                        {hasChildren ? <ChevronDown className={cn("ml-auto size-3.5! text-subtle-foreground transition-transform duration-150", !active && "-rotate-90")} aria-hidden="true" /> : null}
                      </SidebarMenuButton>
                      {showValue ? <SidebarMenuBadge className="top-1.5! right-2 h-5 min-w-5 rounded-full bg-warning-soft px-1.5 text-[0.6875rem] text-warning-foreground!" aria-label={zh ? `${item.value} 项待处理` : `${item.value} open issues`}>{item.value}</SidebarMenuBadge> : null}
                      {hasChildren && active ? (
                        <SidebarMenuSub className="mx-0 gap-px border-l-0 px-0 py-0.5">
                          {item.children!.map((child) => {
                            const current = activeChild === child;
                            return (
                              <SidebarMenuSubItem key={child.href}>
                                <SidebarMenuSubButton
                                  size="md"
                                  isActive={current}
                                  title={child.detail}
                                  className={cn("h-8 pl-[2.375rem] text-[0.8125rem] text-muted-foreground hover:text-foreground", current && "font-medium text-foreground")}
                                  render={<a {...spaLink(child.href, navigatePath, closeMobile)} aria-current={current ? "page" : undefined} />}
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

      <SidebarFooter className="gap-0.5 border-t border-sidebar-border p-2.5 group-data-[collapsible=icon]:p-2">
        {showFleetSummary && vm.nodes.length ? (
          <a
            {...spaLink("/fleet", navigatePath, closeMobile)}
            className="flex h-8 items-center gap-2 rounded-md px-2.5 text-xs text-muted-foreground outline-none hover:bg-sidebar-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring group-data-[collapsible=icon]:hidden"
          >
            <span className={cn("size-1.5 shrink-0 rounded-full", fleetTone)} aria-hidden="true" />
            <span className="truncate">
              {zh ? `${vm.activeNodes}/${vm.nodes.length} 节点就绪` : `${vm.activeNodes}/${vm.nodes.length} nodes ready`}
              {unhealthy ? (zh ? ` · ${unhealthy} 个服务异常` : ` · ${unhealthy} unhealthy`) : ""}
            </span>
          </a>
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
          <span className="group-data-[collapsible=icon]:hidden">{zh ? "收起侧边栏" : "Collapse"}</span>
        </button>
      </SidebarFooter>
    </Sidebar>
  );
}
