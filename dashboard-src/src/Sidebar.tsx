import { PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { buildNavGroups, type NavGroup } from "./navItems";
import type { DashboardViewModel, NavPage } from "./dashboardViewModel";
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
              return (
                <button
                  className={activeNavPage === item.id ? "nav-item active" : "nav-item"}
                  type="button"
                  key={item.id}
                  title={sidebarCollapsed ? item.label : undefined}
                  onClick={() => onNavigate(item.id)}
                >
                  <Icon size={17} aria-hidden="true" />
                  <span>
                    <b>{item.label}</b>
                    <small>{item.detail}</small>
                  </span>
                  <strong>{item.value}</strong>
                </button>
              );
            })}
          </div>
        ))}
      </nav>
      <div className="sidebar-status" aria-label={lang === "zh" ? "当前运行状态" : "Current runtime status"}>
        <span>{lang === "zh" ? "健康分" : "Health score"}</span>
        <strong>{vm.healthScore}%</strong>
        <small>{vm.activeNodes}/{vm.nodes.length || 0} {lang === "zh" ? "节点在线" : "nodes online"}</small>
      </div>
    </aside>
  );
}
