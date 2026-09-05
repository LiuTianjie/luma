import type { DetailState } from "./detailRecords";
import type { Lang } from "./types";
import { detailLabel, t } from "./i18n";
import { OverlayShell } from "./useOverlay";

// Units follow nodeDetail's explicit metric mapping. Other fields retain their
// original representation; load1 is not a percentage and CPU capacity is not usage.
function displayValue(kind: "node" | "service", key: string, value: string | number | boolean | undefined): string {
  if (kind === "node" && typeof value === "number") {
    if (["cpu", "memory"].includes(key)) return Number.isFinite(value) ? `${value}%` : "-";
    if (["memoryTotal", "memoryCapacity"].includes(key)) {
      if (!Number.isFinite(value) || value < 0) return "-";
      if (value === 0) return "0 B";
      const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
      const unit = Math.min(Math.max(0, Math.floor(Math.log(value) / Math.log(1024))), units.length - 1);
      return `${Number((value / 1024 ** unit).toFixed(2))} ${units[unit]}`;
    }
  }
  return String(value ?? "-");
}

export function DetailDrawer({ lang, detail, onClose, inline = false, showBack = true }: { lang: Lang; detail: DetailState; onClose: () => void; inline?: boolean; showBack?: boolean }) {
  if (!detail) return null;
  if (inline) return (
    <section className="detail-page panel" aria-labelledby="detail-page-title">
      <header className="panel-heading">
        <div><p className="eyebrow">{t(lang, "details")}</p><h1 id="detail-page-title">{detail.title}</h1></div>
        {showBack && <button type="button" className="ghost" onClick={onClose}>{lang === "zh" ? "返回列表" : "Back to list"}</button>}
      </header>
      <dl className="detail-properties">
        {Object.entries(detail.items).map(([key, value]) => <div key={key}><dt>{detailLabel(lang, key)}</dt><dd>{displayValue(detail.kind, key, value)}</dd></div>)}
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
                <dd>{displayValue(detail.kind, key, value)}</dd>
              </div>
            ))}
          </dl>
        </aside>
      )}
    </OverlayShell>
  );
}
