import type { ReactNode } from "react";

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
};

export function PageHeader({ meta }: { meta: PageHeaderMeta }) {
  return (
    <section className="page-toolbar" aria-labelledby="page-title">
      <div className="page-toolbar-copy">
        <p className="eyebrow">{meta.eyebrow}</p>
        <h1 id="page-title">{meta.title}</h1>
        <p>{meta.description}</p>
      </div>
      <div className="page-toolbar-side">
        <div className="page-toolbar-metrics" aria-label="Page metrics">
          {meta.metrics.map((metric) => (
            <span key={metric.label}>
              <strong>{metric.value}</strong>
              <small>{metric.label}</small>
            </span>
          ))}
        </div>
        {meta.action ? <div className="page-toolbar-action">{meta.action}</div> : null}
      </div>
    </section>
  );
}
