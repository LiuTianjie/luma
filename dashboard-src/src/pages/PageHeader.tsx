import type { CSSProperties, ReactNode } from "react";

export type PageMetric = {
  label: string;
  value: string | number;
};

export type PageHeaderMeta = {
  eyebrow: string;
  title: string;
  description: string;
  metrics: PageMetric[];
  action?: ReactNode;
  /** Overview-style header with health score ring */
  variant?: "default" | "ops";
  score?: {
    value: number;
    label: string;
    status: string;
  };
};

export function PageHeader({ meta }: { meta: PageHeaderMeta }) {
  const isOps = meta.variant === "ops";
  return (
    <section
      className={isOps ? "page-toolbar ops" : "page-toolbar"}
      aria-labelledby="page-title"
    >
      <div className="page-toolbar-copy">
        <p className="eyebrow">{meta.eyebrow}</p>
        <h1 id="page-title">{meta.title}</h1>
        <p>{meta.description}</p>
        {isOps && meta.action ? <div className="page-toolbar-action">{meta.action}</div> : null}
      </div>
      <div className={isOps ? "page-toolbar-side ops-side" : "page-toolbar-side"}>
        {meta.score ? (
          <div className="ops-hero-score" aria-label={meta.score.label}>
            <div className="score-ring" style={{ "--score": `${meta.score.value}%` } as CSSProperties}>
              <strong>{meta.score.value}</strong>
            </div>
            <span>
              {meta.score.label}
              <b>{meta.score.status}</b>
            </span>
          </div>
        ) : null}
        {meta.metrics.length ? (
          <div className="page-toolbar-metrics" aria-label="Page metrics">
            {meta.metrics.map((metric) => (
              <span key={metric.label}>
                <strong>{metric.value}</strong>
                <small>{metric.label}</small>
              </span>
            ))}
          </div>
        ) : null}
        {!isOps && meta.action ? <div className="page-toolbar-action">{meta.action}</div> : null}
      </div>
    </section>
  );
}
