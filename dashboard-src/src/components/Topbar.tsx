import { LogOut, Monitor, Moon, RefreshCw, Sun } from "lucide-react";
import { t } from "../i18n";
import type { Lang, SyncStatus } from "../types";
import type { ThemeMode } from "../useTheme";

type Props = {
  clusterId: string;
  lang: Lang;
  lastUpdated: Date | null;
  syncStatus: SyncStatus;
  themeMode: ThemeMode;
  onLangChange: (lang: Lang) => void;
  onThemeModeChange: (mode: ThemeMode) => void;
  onRefresh: () => void;
  onSignOut: () => void;
};

export function Topbar({
  clusterId,
  lang,
  lastUpdated,
  syncStatus,
  themeMode,
  onLangChange,
  onThemeModeChange,
  onRefresh,
  onSignOut,
}: Props) {
  const timeFormatter = new Intl.DateTimeFormat(lang === "zh" ? "zh-CN" : "en-US", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
  const statusText = syncStatus === "updated" && lastUpdated
    ? `${t(lang, "updated")} ${timeFormatter.format(lastUpdated)}`
    : t(lang, syncStatus);
  const themeOptions: Array<{ mode: ThemeMode; icon: typeof Sun; label: string }> = [
    { mode: "system", icon: Monitor, label: lang === "zh" ? "跟随系统" : "Follow system" },
    { mode: "light", icon: Sun, label: lang === "zh" ? "日间模式" : "Light mode" },
    { mode: "dark", icon: Moon, label: lang === "zh" ? "夜间模式" : "Dark mode" },
  ];

  return (
    <header className="topbar">
      <div className="cluster-chip">
        <span>{t(lang, "cluster")}</span>
        <strong translate="no">{clusterId}</strong>
      </div>
      <div className="top-actions">
        <span className={`sync-state ${syncStatus}`}>{statusText}</span>
        <div className="lang-switch theme-switch" role="group" aria-label={lang === "zh" ? "主题" : "Theme"}>
          {themeOptions.map(({ mode, icon: Icon, label }) => (
            <button
              key={mode}
              className={themeMode === mode ? "active" : ""}
              type="button"
              title={label}
              aria-label={label}
              aria-pressed={themeMode === mode}
              onClick={() => onThemeModeChange(mode)}
            >
              <Icon size={16} aria-hidden="true" />
            </button>
          ))}
        </div>
        <div className="lang-switch" aria-label="Language">
          <button className={lang === "zh" ? "active" : ""} onClick={() => onLangChange("zh")} type="button">中文</button>
          <button className={lang === "en" ? "active" : ""} onClick={() => onLangChange("en")} type="button">EN</button>
        </div>
        <button type="button" onClick={onRefresh}>
          <RefreshCw size={16} aria-hidden="true" />
          {t(lang, "refresh")}
        </button>
        <button className="ghost" type="button" onClick={onSignOut}>
          <LogOut size={16} aria-hidden="true" />
          {t(lang, "signOut")}
        </button>
      </div>
    </header>
  );
}
