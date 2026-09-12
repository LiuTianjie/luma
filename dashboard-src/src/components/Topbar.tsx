import { parseApplicationPath, applicationPath } from "./applicationRoutes";
import { ArrowLeft, Package } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Breadcrumb, BreadcrumbList, BreadcrumbItem, BreadcrumbLink, BreadcrumbPage, BreadcrumbSeparator } from "@/components/ui/breadcrumb";
import { buildNavGroups } from "../navItems";
import { ROUTE_BY_PAGE } from "../routes";
import { toHref, useRouter } from "../router";
import type { DashboardViewModel, NavPage } from "../dashboardViewModel";
import { SidebarTrigger } from "@/components/ui/sidebar";
import { t } from "../i18n";
import type { Lang, SyncStatus } from "../types";

type Props = {
  vm: DashboardViewModel;
  activeNavPage: NavPage;
  lang: Lang;
  lastUpdated: Date | null;
  syncStatus: SyncStatus;
};

export function Topbar({
  vm,
  activeNavPage,
  lang,
  lastUpdated,
  syncStatus,
}: Props) {
  const { path, navigate } = useRouter();
  const applicationRoute = parseApplicationPath(path);
  const items = buildNavGroups(lang, vm).flatMap(group => group.items);
  const item = items.find(item => item.children?.some(child => path === child.href || path.startsWith(`${child.href}/`)))
    || items.find(item => item.id === activeNavPage);
  const child = item?.children?.filter(child => path === child.href || path.startsWith(`${child.href}/`)).sort((a, b) => b.href.length - a.href.length)[0];
  const title = child?.label || item?.label || (activeNavPage === "deploy" ? (lang === "zh" ? "创建应用" : "Create application") : activeNavPage === "builder" ? (lang === "zh" ? "构建" : "Builds") : (lang === "zh" ? "控制台" : "Console"));
  const Icon = item?.icon || (["deploy", "builder"].includes(activeNavPage) ? Package : undefined);
  const timeFormatter = new Intl.DateTimeFormat(lang === "zh" ? "zh-CN" : "en-US", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
  const statusText = syncStatus === "updated" && lastUpdated
    ? `${t(lang, "updated")} ${timeFormatter.format(lastUpdated)}`
    : t(lang, syncStatus);

  return (
    <header className="console-topbar">
      <SidebarTrigger className="md:hidden" aria-label={lang === "zh" ? "打开导航" : "Open navigation"} />
      {path.startsWith("/create/") && <Button variant="ghost" size="sm" onClick={() => navigate("/create")} aria-label={lang === "zh" ? "返回创建" : "Back to create"}><ArrowLeft />{lang === "zh" ? "返回" : "Back"}</Button>}
      <Breadcrumb className="console-breadcrumb" aria-label={lang === "zh" ? "当前位置" : "Current location"}>
        <BreadcrumbList>
          {applicationRoute.stack ? <>
            <BreadcrumbItem><BreadcrumbLink href={toHref("/apps")} onClick={event => { if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return; event.preventDefault(); navigate("/apps"); }}>{lang === "zh" ? "应用" : "Applications"}</BreadcrumbLink></BreadcrumbItem>
            <BreadcrumbSeparator />
            <BreadcrumbItem>{applicationRoute.service ? <BreadcrumbLink href={toHref(applicationPath(applicationRoute.stack))}>{applicationRoute.stack}</BreadcrumbLink> : <BreadcrumbPage>{applicationRoute.stack}</BreadcrumbPage>}</BreadcrumbItem>
            {applicationRoute.service && <><BreadcrumbSeparator /><BreadcrumbItem><BreadcrumbPage>{applicationRoute.service}</BreadcrumbPage></BreadcrumbItem></>}
          </> : <>
          {child && item ? <><BreadcrumbItem><BreadcrumbLink href={toHref(ROUTE_BY_PAGE[item.id])} onClick={event => {
            if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
            event.preventDefault(); navigate(ROUTE_BY_PAGE[item.id]);
          }}>{item.label}</BreadcrumbLink></BreadcrumbItem><BreadcrumbSeparator /></> : null}
          <BreadcrumbItem>{Icon ? <Icon className="size-4 shrink-0" aria-hidden="true" /> : null}<BreadcrumbPage>{title}</BreadcrumbPage></BreadcrumbItem>
          </>}

        </BreadcrumbList>
      </Breadcrumb>
      <div className="ml-auto flex items-center gap-1.5">
        <span className="hidden text-xs text-muted-foreground xl:inline" title={statusText} role="status">
          {statusText}
        </span>

      </div>
    </header>
  );
}
