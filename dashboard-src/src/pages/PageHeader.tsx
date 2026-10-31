import type { ReactNode } from "react";
import { Progress, ProgressLabel, ProgressValue } from "@/components/ui/progress";
import { cn } from "@/lib/utils";

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

/**
 * One heading per page, actions on the right (Kumo ResourceListPage). The sidebar
 * and breadcrumb already name the workspace, so the title carries no icon or
 * eyebrow. Metrics are plain facts in a single quiet line, not chips that look
 * clickable.
 */
export function PageHeader({ meta }: { meta: PageHeaderMeta }) {
  return (
    <section className="flex min-w-0 flex-wrap items-start justify-between gap-x-6 gap-y-4" aria-labelledby="page-title">
      <div className="flex min-w-0 flex-1 basis-72 flex-col gap-1.5">
        <h1 id="page-title" className={cn("min-w-0 font-heading font-semibold tracking-[-0.02em] wrap-anywhere", meta.variant === "ops" ? "text-xl" : "text-[1.625rem]/8")}>{meta.title}</h1>
        {meta.description ? <p className="max-w-3xl text-sm/6 text-pretty text-muted-foreground">{meta.description}</p> : null}
        {meta.metrics.length ? (
          <dl className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-[0.8125rem]" aria-label={meta.title}>
            {meta.metrics.map((metric, index) => (
              <div key={metric.label} className="flex items-center gap-1.5">
                {index ? <span aria-hidden="true" className="-ml-2 mr-0.5 h-3 w-px bg-border" /> : null}
                <dt className="text-muted-foreground">{metric.label}</dt>
                <dd className="font-medium tabular-nums">{metric.value}</dd>
              </div>
            ))}
          </dl>
        ) : null}
      </div>
      {meta.action || meta.score ? <div className="flex max-w-full flex-wrap items-center gap-2 pt-0.5">
        {meta.score ? <div className="flex w-48 flex-col gap-1">
          <Progress value={meta.score.value}>
            <ProgressLabel>{meta.score.label}</ProgressLabel>
            <ProgressValue />
          </Progress>
          <p className="text-sm text-muted-foreground">{meta.score.status}</p>
        </div> : null}
        {meta.action}
      </div> : null}
    </section>
  );
}
