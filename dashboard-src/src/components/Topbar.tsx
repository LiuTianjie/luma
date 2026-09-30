import { parseObjectRoute } from "../objectRoutes";
import { parseApplicationPath, applicationPath } from "./applicationRoutes";
import { ArrowLeft } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Breadcrumb, BreadcrumbList, BreadcrumbItem, BreadcrumbLink, BreadcrumbPage, BreadcrumbSeparator } from "@/components/ui/breadcrumb";
import { activeNavChild, buildNavGroups, navWorkspace } from "../navItems";
import { ROUTE_BY_PAGE } from "../routes";
import { useRouter, spaLink } from "../router";
import type { DashboardViewModel, NavPage } from "../dashboardViewModel";
import { SidebarTrigger } from "@/components/ui/sidebar";
import { t } from "../i18n";
import type { Lang, SyncStatus } from "../types";

type Props = {
  vm: DashboardViewModel;
  activeNavPage: NavPage | null;
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
  // Node and service detail pages name the object as the last crumb.
  const objectRoute = parseObjectRoute(path);
  const objectName = objectRoute?.kind === "node" || objectRoute?.kind === "service" ? objectRoute.name : "";
  const item = activeNavPage ? buildNavGroups(lang, vm).flatMap(group => group.items).find(entry => entry.id === navWorkspace(activeNavPage)) : undefined;
  const child = activeNavChild(item, path);
  // Task pages without a sidebar entry still name themselves under their owning workspace.
  const pageTitle = activeNavPage === "deploy" ? (lang === "zh" ? "创建应用" : "Create application")
    : activeNavPage === "builder" ? (lang === "zh" ? "从 Git 构建" : "Build from Git") : "";
  const leaf = child?.label || pageTitle;
  const title = leaf || item?.label || (activeNavPage ? (lang === "zh" ? "控制台" : "Console") : (lang === "zh" ? "页面不存在" : "Page not found"));
  const Icon = item?.icon;
  const timeFormatter = new Intl.DateTimeFormat(lang === "zh" ? "zh-CN" : "en-US", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
  // Pages that load their own data have no snapshot time; say nothing rather than a bare "Updated".
  const statusText = syncStatus === "updated"
    ? lastUpdated ? `${t(lang, "updated")} ${timeFormatter.format(lastUpdated)}` : ""
    : t(lang, syncStatus);

  return (
    <header className="flex h-(--console-header-height) shrink-0 items-center gap-3 border-b px-4 md:px-6">
      <SidebarTrigger className="md:hidden" aria-label={lang === "zh" ? "打开导航" : "Open navigation"} />
      {path.startsWith("/create/") && <Button variant="ghost" size="sm" onClick={() => navigate("/create")} aria-label={lang === "zh" ? "返回创建" : "Back to create"}><ArrowLeft data-icon="inline-start" />{lang === "zh" ? "返回" : "Back"}</Button>}
      <Breadcrumb className="min-w-0" aria-label={lang === "zh" ? "当前位置" : "Current location"}>
        <BreadcrumbList className="flex-nowrap">
          {applicationRoute.stack ? <>
            <BreadcrumbItem><BreadcrumbLink {...spaLink("/apps", navigate)}>{lang === "zh" ? "应用" : "Applications"}</BreadcrumbLink></BreadcrumbItem>
            <BreadcrumbSeparator />
            <BreadcrumbItem className="min-w-0">{applicationRoute.service ? <BreadcrumbLink {...spaLink(applicationPath(applicationRoute.stack), navigate)} className="truncate" title={applicationRoute.stack}>{applicationRoute.stack}</BreadcrumbLink> : <BreadcrumbPage className="truncate" title={applicationRoute.stack}>{applicationRoute.stack}</BreadcrumbPage>}</BreadcrumbItem>
            {applicationRoute.service && <><BreadcrumbSeparator /><BreadcrumbItem className="min-w-0"><BreadcrumbPage className="truncate" title={applicationRoute.service}>{applicationRoute.service}</BreadcrumbPage></BreadcrumbItem></>}
          </> : <>
          {(leaf || objectName) && item ? <><BreadcrumbItem><BreadcrumbLink {...spaLink(ROUTE_BY_PAGE[item.id], navigate)}>{item.label}</BreadcrumbLink></BreadcrumbItem><BreadcrumbSeparator /></> : null}
          {objectName ? <>
            {child ? <><BreadcrumbItem><BreadcrumbLink {...spaLink(child.href, navigate)}>{child.label}</BreadcrumbLink></BreadcrumbItem><BreadcrumbSeparator /></> : null}
            <BreadcrumbItem className="min-w-0"><BreadcrumbPage className="truncate" title={objectName}>{objectName}</BreadcrumbPage></BreadcrumbItem>
          </> : <BreadcrumbItem>{Icon ? <Icon aria-hidden="true" /> : null}<BreadcrumbPage>{title}</BreadcrumbPage></BreadcrumbItem>}
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
