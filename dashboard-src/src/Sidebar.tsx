import { PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { buildNavGroups, type NavGroup } from "./navItems";
import type { DashboardViewModel, NavPage } from "./dashboardViewModel";
import { ROUTE_BY_PAGE } from "./routes";
import { toHref } from "./router";
import type { Lang } from "./types";
import { t } from "./i18n";
import lumaLogoMark from "./assets/luma-logo-mark.png";

export function Sidebar({
  lang,
  vm,
  activeNavPage,
  sidebarCollapsed,
  onNavigate,
  onToggle,
}: {
  lang: Lang;
  vm: DashboardViewModel;
  activeNavPage: NavPage;
  sidebarCollapsed: boolean;
  onNavigate: (page: NavPage) => void;
  onToggle: () => void;
}) {
  const groups: NavGroup[] = buildNavGroups(lang, vm);
  const sidebarToggleLabel = sidebarCollapsed
    ? (lang === "zh" ? "展开侧栏" : "Expand sidebar")
    : (lang === "zh" ? "收起侧栏" : "Collapse sidebar");

  return (
    <aside className="sidebar">
      <div className="sidebar-brand">
        <div className="brand-mark" aria-hidden="true">
          <img src={lumaLogoMark} alt="" />
        </div>
        <div className="sidebar-title">
          <span>Luma</span>
          <strong>{t(lang, "title")}</strong>
        </div>
        <button
          type="button"
          className="sidebar-toggle"
          title={sidebarToggleLabel}
          aria-label={sidebarToggleLabel}
          aria-expanded={!sidebarCollapsed}
          onClick={onToggle}
        >
          {sidebarCollapsed ? <PanelLeftOpen size={16} aria-hidden="true" /> : <PanelLeftClose size={16} aria-hidden="true" />}
        </button>
      </div>
      <nav aria-label="Dashboard">
        {groups.map((group) => (
          <div className="nav-group" key={group.key}>
            {group.label ? <p className="nav-section">{group.label}</p> : null}
            {group.items.map((item) => {
              const Icon = item.icon;
              const showValue = typeof item.value === "number";
              const active = activeNavPage === item.id;
              const tip = sidebarCollapsed ? `${item.label} - ${item.detail}` : item.detail;
              return (
                <a
                  className={active ? "nav-item active" : "nav-item"}
                  key={item.id}
                  href={toHref(ROUTE_BY_PAGE[item.id])}
                  title={tip}
                  aria-current={active ? "page" : undefined}
                  onClick={(event) => {
                    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
                    event.preventDefault();
                    onNavigate(item.id);
                  }}
                >
                  <Icon size={16} aria-hidden="true" />
                  <span>
                    <b>{item.label}</b>
                    <small>{item.detail}</small>
                  </span>
                  {showValue ? <strong>{item.value}</strong> : null}
                </a>
              );
            })}
          </div>
        ))}
      </nav>
      <div className="sidebar-status" aria-label={lang === "zh" ? "当前运行状态" : "Current runtime status"}>
        <span>{lang === "zh" ? "就绪节点" : "Ready nodes"}</span>
        <strong>{vm.activeNodes}<small> / {vm.nodes.length}</small></strong>
        <small>{lang === "zh"
          ? `${Math.max(0, vm.services.length - vm.healthyServices)} 个服务异常`
          : `${Math.max(0, vm.services.length - vm.healthyServices)} services need attention`}</small>
      </div>
    </aside>
  );
}
