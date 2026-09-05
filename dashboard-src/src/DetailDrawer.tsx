import type { DetailState } from "./detailRecords";
import type { Lang } from "./types";
import { detailLabel, t } from "./i18n";
import { OverlayShell } from "./useOverlay";

export function DetailDrawer({ lang, detail, onClose, inline = false }: { lang: Lang; detail: DetailState; onClose: () => void; inline?: boolean }) {
  if (!detail) return null;
  if (inline) return (
    <section className="detail-page panel" aria-labelledby="detail-page-title">
      <header className="panel-heading">
        <div><p className="eyebrow">{t(lang, "details")}</p><h1 id="detail-page-title">{detail.title}</h1></div>
        <button type="button" className="ghost" onClick={onClose}>{lang === "zh" ? "返回列表" : "Back to list"}</button>
      </header>
      <dl className="detail-properties">
        {Object.entries(detail.items).map(([key, value]) => <div key={key}><dt>{detailLabel(lang, key)}</dt><dd>{String(value ?? "-")}</dd></div>)}
      </dl>
    </section>
  );
  return (
    <OverlayShell<HTMLElement> className="detail-backdrop" onClose={onClose}>
      {(overlayRef) => (
        <aside
          className="detail-drawer"
          ref={overlayRef}
          role="dialog"
          aria-modal="true"
          aria-labelledby="detail-drawer-title"
          onClick={(event) => event.stopPropagation()}
        >
          <header>
            <div>
              <p className="eyebrow">{t(lang, "details")}</p>
              <h2 id="detail-drawer-title">{detail.title}</h2>
            </div>
            <button type="button" className="icon-button" onClick={onClose}>
              {t(lang, "close")}
            </button>
          </header>
          <dl>
            {Object.entries(detail.items).map(([key, value]) => (
              <div key={key}>
                <dt>{detailLabel(lang, key)}</dt>
                <dd>{String(value ?? "-")}</dd>
              </div>
            ))}
          </dl>
        </aside>
      )}
    </OverlayShell>
  );
}
