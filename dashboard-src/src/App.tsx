import { useEffect, useMemo, useState } from "react";
import { ErrorBanner } from "./components/ErrorBanner";
import { LoginPanel } from "./components/LoginPanel";
import { NodeTopology } from "./components/NodeTopology";
import { NodesTable } from "./components/NodesTable";
import { ReadinessCards } from "./components/ReadinessCards";
import { ServicesTable } from "./components/ServicesTable";
import { StoragePanel } from "./components/StoragePanel";
import { Topbar } from "./components/Topbar";
import { TrafficPaths } from "./components/TrafficPaths";
import { t } from "./i18n";
import type { DashboardNode, DashboardService, Lang, SyncStatus } from "./types";
import { useDashboardData } from "./useDashboardData";

const LANG_KEY = "luma.dashboard.lang";

type DetailState =
  | { kind: "node"; title: string; items: Record<string, string | number | boolean | undefined> }
  | { kind: "service"; title: string; items: Record<string, string | number | boolean | undefined> }
  | null;

export function App() {
  const [lang, setLangState] = useState<Lang>(() => (localStorage.getItem(LANG_KEY) === "en" ? "en" : "zh"));
  const [detail, setDetail] = useState<DetailState>(null);
  const [theme, setThemeState] = useState<"light" | "dark">(() => {
    const saved = localStorage.getItem("luma.dashboard.theme");
    if (saved === "light" || saved === "dark") return saved;
    return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  });
  const { token, payload, errors, syncStatus, lastUpdated, setToken, signOut, loadDashboard } = useDashboardData();

  useEffect(() => {
    document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  }, [lang]);

  useEffect(() => {
    if (theme === "light") {
      document.documentElement.classList.add("light");
    } else {
      document.documentElement.classList.remove("light");
    }
  }, [theme]);

  const setLang = (nextLang: Lang) => {
    setLangState(nextLang);
    localStorage.setItem(LANG_KEY, nextLang);
  };

  const toggleTheme = () => {
    const nextTheme = theme === "light" ? "dark" : "light";
    setThemeState(nextTheme);
    localStorage.setItem("luma.dashboard.theme", nextTheme);
  };

  const visibleStatus: SyncStatus = token ? syncStatus : "notConnected";
  const clusterId = payload?.cluster?.id || "-";
  const nodes = payload?.nodes || [];
  const services = payload?.services || [];
  const paths = payload?.trafficPaths || [];
  const storageVolumes = payload?.storage?.volumes || [];
  const storageClasses = payload?.storage?.storageClasses || [];
  const storageWarnings = payload?.storage?.warnings || [];

  const navItems = useMemo(
    () => [
      { label: t(lang, "navOverview"), value: clusterId },
      { label: t(lang, "navNodes"), value: nodes.length },
      { label: t(lang, "navServices"), value: services.length },
      { label: t(lang, "navTopology"), value: nodes.length },
      { label: t(lang, "navTraffic"), value: paths.length },
      { label: t(lang, "navStorage"), value: storageVolumes.length + storageClasses.length },
    ],
    [clusterId, lang, nodes.length, paths.length, services.length, storageClasses.length, storageVolumes.length],
  );

  const openNodeDetail = (node: DashboardNode) => {
    setDetail({
      kind: "node",
      title: node.name || "-",
      items: {
        displayName: node.displayName,
        region: node.region,
        role: node.role,
        state: node.state,
        availability: node.availability,
        leader: node.leader,
      },
    });
  };

  const openServiceDetail = (service: DashboardService) => {
    setDetail({
      kind: "service",
      title: service.stack ? `${service.stack}/${service.name || "-"}` : service.name || "-",
      items: {
        fullName: service.fullName,
        region: service.region,
        exposure: service.exposure,
        image: service.image,
        replicas: `${service.running ?? 0}/${service.desired ?? 0}`,
        pending: service.pending,
        failed: service.failed,
        health: service.health,
        nodes: (service.nodes || []).join(", "),
        storage: (service.storage || []).map((item) => `${item.name || "-"}:${item.kind || "unmanaged"}`).join(", "),
        diagnostics: (service.diagnostics || []).join("; "),
      },
    });
  };

  return (
    <div className="dashboard-shell">
      <aside className="sidebar">
        <div className="brand-mark" aria-hidden="true">L</div>
        <div className="sidebar-title">
          <span>Luma Control</span>
          <strong>{t(lang, "title")}</strong>
        </div>
        <nav aria-label="Dashboard">
          {navItems.map((item, index) => (
            <a className={index === 0 ? "active" : ""} href={`#section-${index}`} key={item.label}>
              <span>{item.label}</span>
              <strong>{item.value}</strong>
            </a>
          ))}
          <a className="muted-link" href="#future">
            <span>{t(lang, "navFuture")}</span>
            <strong>{t(lang, "reserved")}</strong>
          </a>
        </nav>
      </aside>

      <main className="workspace">
        <div className="topbar-wrapper">
          <Topbar
            clusterId={clusterId}
            lang={lang}
            lastUpdated={lastUpdated}
            onLangChange={setLang}
            onRefresh={() => void loadDashboard()}
            onSignOut={signOut}
            syncStatus={visibleStatus}
            theme={theme}
            onThemeToggle={toggleTheme}
          />
        </div>

        {!token ? (
          <div className="login-panel-container">
            <LoginPanel lang={lang} onSubmit={setToken} />
          </div>
        ) : (
          <>
            <section className="hero-strip" id="section-0">
              <div>
                <p className="eyebrow">{t(lang, "controlPlane")}</p>
                <h1>{t(lang, "title")}</h1>
                <p>{t(lang, "subtitle")}</p>
              </div>
              <div className="hero-metrics" aria-label="Cluster summary">
                <span>{nodes.length} {t(lang, "nodes")}</span>
                <span>{services.length} {t(lang, "services")}</span>
                <span>{paths.length} {t(lang, "trafficPaths")}</span>
              </div>
            </section>

            <ErrorBanner errors={errors} />
            {payload ? (
              <>
                <ReadinessCards lang={lang} payload={payload} />
                <section className="table-grid">
                  <NodesTable lang={lang} nodes={nodes} onSelect={openNodeDetail} />
                  <ServicesTable lang={lang} services={services} onSelect={openServiceDetail} />
                </section>
                <NodeTopology lang={lang} nodes={nodes} services={services} theme={theme} />
                <TrafficPaths lang={lang} paths={paths} theme={theme} />
                <StoragePanel lang={lang} volumes={storageVolumes} storageClasses={storageClasses} warnings={storageWarnings} />
              </>
            ) : (
              <section className="empty-state">
                <p>{t(lang, visibleStatus)}</p>
              </section>
            )}
          </>
        )}
      </main>

      {detail ? (
        <div className="detail-backdrop" onClick={() => setDetail(null)}>
          <aside className="detail-drawer" onClick={(event) => event.stopPropagation()}>
            <header>
              <div>
                <p className="eyebrow">{t(lang, "details")}</p>
                <h2>{detail.title}</h2>
              </div>
              <button type="button" className="icon-button" onClick={() => setDetail(null)}>
                {t(lang, "close")}
              </button>
            </header>
            <dl>
              {Object.entries(detail.items).map(([key, value]) => (
                <div key={key}>
                  <dt>{key}</dt>
                  <dd>{String(value || "-")}</dd>
                </div>
              ))}
            </dl>
          </aside>
        </div>
      ) : null}
    </div>
  );
}
