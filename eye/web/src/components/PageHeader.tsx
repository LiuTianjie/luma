import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export interface PageMetric {
  label: string;
  value: ReactNode;
}

export interface PageHeaderProps {
  title: string;
  description?: string;
  metrics?: PageMetric[];
  action?: ReactNode;
  compact?: boolean;
}

export function PageHeader({ title, description, metrics = [], action, compact }: PageHeaderProps) {
  return (
    <section
      className="flex min-w-0 flex-wrap items-start justify-between gap-x-6 gap-y-3"
      aria-labelledby="page-title"
    >
      <div className="flex min-w-0 flex-1 basis-72 flex-col gap-1">
        <h1
          id="page-title"
          className={cn(
            "min-w-0 font-heading font-semibold tracking-[-0.02em] wrap-anywhere",
            compact ? "text-xl/7" : "text-[1.5rem]/8",
          )}
        >
          {title}
        </h1>
        {description ? (
          <p className="max-w-3xl text-sm/6 text-pretty text-muted-foreground">{description}</p>
        ) : null}
        {metrics.length > 0 ? (
          <dl
            className="mt-0.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-[0.8125rem]"
            aria-label={title}
          >
            {metrics.map((metric, index) => (
              <div key={metric.label} className="flex items-center gap-1.5">
                {index > 0 ? (
                  <span aria-hidden="true" className="-ml-2 mr-0.5 h-3 w-px bg-border" />
                ) : null}
                <dt className="text-muted-foreground">{metric.label}</dt>
                <dd className="font-medium tabular-nums">{metric.value}</dd>
              </div>
            ))}
          </dl>
        ) : null}
      </div>
      {action ? <div className="flex max-w-full flex-wrap items-center gap-2 pt-0.5">{action}</div> : null}
    </section>
  );
}
