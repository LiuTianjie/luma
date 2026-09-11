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
  variant?: "default" | "ops";
  score?: {
    value: number;
    label: string;
    status: string;
  };
};

export function PageHeader({ meta }: { meta: PageHeaderMeta }) {
  return (
    <section className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between" aria-labelledby="page-title">
      <div className="flex min-w-0 flex-col gap-2">
        <p className="text-xs font-medium tracking-wide text-muted-foreground uppercase">{meta.eyebrow}</p>
        <h1 id="page-title" className="font-heading text-2xl font-medium tracking-tight">{meta.title}</h1>
        {meta.description ? <p className="max-w-2xl text-sm text-muted-foreground">{meta.description}</p> : null}
      </div>
      <div className="flex flex-wrap items-center gap-3">
        {meta.score ? (
          <div className="flex items-center gap-3" aria-label={meta.score.label}>
            <div
              className="score-ring flex size-14 items-center justify-center rounded-full text-lg font-semibold"
              style={{ "--score": `${meta.score.value}%` } as CSSProperties}
            >
              <strong>{meta.score.value}</strong>
            </div>
            <span className="flex flex-col text-sm">
              {meta.score.label}
              <b>{meta.score.status}</b>
            </span>
          </div>
        ) : null}
        {meta.metrics.length ? (
          <div className="flex flex-wrap gap-2" aria-label={meta.title}>
            {meta.metrics.map((metric) => (
              <span key={metric.label} className="flex min-w-20 flex-col rounded-lg border bg-card px-3 py-2">
                <strong className="text-lg font-medium leading-none">{metric.value}</strong>
                <small className="mt-1 text-xs text-muted-foreground">{metric.label}</small>
              </span>
            ))}
          </div>
        ) : null}
        {meta.action ? <div>{meta.action}</div> : null}
      </div>
    </section>
  );
}
