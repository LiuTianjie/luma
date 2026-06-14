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
};

export function Topbar({
  clusterId,
  lang,
  lastUpdated,
  syncStatus,
  onLangChange,
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

  return (
    <header className="topbar">
      <div className="cluster-chip">
        <span>{t(lang, "cluster")}</span>
        <strong translate="no">{clusterId}</strong>
      </div>
      <div className="top-actions">
        <span className={`sync-state ${syncStatus}`}>{statusText}</span>
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
