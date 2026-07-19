import type { DetailState } from "./detailRecords";
import type { Lang } from "./types";
import { detailLabel, t } from "./i18n";

export function DetailDrawer({ lang, detail, onClose }: { lang: Lang; detail: DetailState; onClose: () => void }) {
  if (!detail) return null;
  return (
    <div className="detail-backdrop" onClick={onClose}>
      <aside className="detail-drawer" onClick={(event) => event.stopPropagation()}>
        <header>
          <div>
            <p className="eyebrow">{t(lang, "details")}</p>
            <h2>{detail.title}</h2>
          </div>
          <button type="button" className="icon-button" onClick={onClose}>
            {t(lang, "close")}
          </button>
        </header>
        <dl>
          {Object.entries(detail.items).map(([key, value]) => (
            <div key={key}>
              <dt>{detailLabel(lang, key)}</dt>
              <dd>{String(value || "-")}</dd>
            </div>
          ))}
        </dl>
      </aside>
    </div>
  );
}
