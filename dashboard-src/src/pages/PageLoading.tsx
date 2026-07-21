import type { Lang } from "../types";

/** Suspense / first-paint placeholder while a route chunk (or page data) loads. */
export function PageLoading({ lang = "zh" }: { lang?: Lang }) {
  const label = lang === "zh" ? "页面加载中…" : "Loading page…";
  return (
    <section className="page-loading" aria-busy="true" aria-live="polite">
      <div className="page-loading-toolbar">
        <div className="page-loading-copy">
          <span className="skeleton skeleton-line skeleton-eyebrow" />
          <span className="skeleton skeleton-line skeleton-title" />
          <span className="skeleton skeleton-line skeleton-desc" />
        </div>
        <div className="page-loading-metrics" aria-hidden="true">
          <span className="skeleton skeleton-metric" />
          <span className="skeleton skeleton-metric" />
          <span className="skeleton skeleton-metric" />
          <span className="skeleton skeleton-metric" />
        </div>
      </div>
      <div className="page-loading-panel skeleton" aria-hidden="true">
        <span className="skeleton skeleton-line skeleton-panel-title" />
        <span className="skeleton skeleton-line" />
        <span className="skeleton skeleton-line" />
        <span className="skeleton skeleton-line skeleton-wide" />
        <span className="skeleton skeleton-line" />
        <span className="skeleton skeleton-line skeleton-medium" />
      </div>
      <p className="page-loading-label">{label}</p>
    </section>
  );
}
