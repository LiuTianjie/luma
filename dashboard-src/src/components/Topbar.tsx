import { t } from "../i18n";
import type { Lang, SyncStatus } from "../types";

type Props = {
  clusterId: string;
  lang: Lang;
  lastUpdated: Date | null;
  syncStatus: SyncStatus;
  onLangChange: (lang: Lang) => void;
  onRefresh: () => void;
  onSignOut: () => void;
  theme: "light" | "dark";
  onThemeToggle: () => void;
};

export function Topbar({
  clusterId,
  lang,
  lastUpdated,
  syncStatus,
  onLangChange,
  onRefresh,
  onSignOut,
  theme,
  onThemeToggle,
}: Props) {
  const statusText = syncStatus === "updated" && lastUpdated
    ? `${t(lang, "updated")} ${lastUpdated.toLocaleTimeString()}`
    : t(lang, syncStatus);

  return (
    <header className="topbar">
      <div className="cluster-chip">
        <span>{t(lang, "cluster")}</span>
        <strong>{clusterId}</strong>
      </div>
      <div className="top-actions">
        <span className={`sync-state ${syncStatus}`}>{statusText}</span>
        <button
          className="theme-toggle-btn"
          onClick={onThemeToggle}
          type="button"
          title={theme === "light" ? "Switch to Dark Mode" : "Switch to Light Mode"}
          aria-label={theme === "light" ? "Switch to Dark Mode" : "Switch to Light Mode"}
        >
          {theme === "light" ? (
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>
            </svg>
          ) : (
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="12" cy="12" r="4"/>
              <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/>
            </svg>
          )}
        </button>
        <div className="lang-switch" aria-label="Language">
          <button className={lang === "zh" ? "active" : ""} onClick={() => onLangChange("zh")} type="button">中文</button>
          <button className={lang === "en" ? "active" : ""} onClick={() => onLangChange("en")} type="button">EN</button>
        </div>
        <button type="button" onClick={onRefresh}>{t(lang, "refresh")}</button>
        <button className="ghost" type="button" onClick={onSignOut}>{t(lang, "signOut")}</button>
      </div>
    </header>
  );
}
