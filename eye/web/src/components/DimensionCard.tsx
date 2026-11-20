import type { ReactNode } from "react";
import { Link } from "react-router";
import type { DimensionCount } from "@/api/types";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";

interface DimensionCardProps {
  title: string;
  description?: string;
  rows: DimensionCount[];
  formatKey?: (key: string) => ReactNode;
  getItemHref?: (key: string) => string | undefined;
  emptyText?: string;
  footerNote?: ReactNode;
  maxItems?: number;
}

export function DimensionCard({
  title,
  description,
  rows,
  formatKey,
  getItemHref,
  emptyText = "暂无分布数据",
  footerNote,
  maxItems = 8,
}: DimensionCardProps) {
  const safeRows = (rows || []).slice(0, maxItems);
  const total = safeRows.reduce((acc, r) => acc + r.count, 0) || 1;
  const maxVal = Math.max(1, ...safeRows.map((r) => r.count));

  return (
    <Card className="min-w-0">
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-semibold">{title}</CardTitle>
        {description ? <CardDescription className="text-xs">{description}</CardDescription> : null}
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        {safeRows.length === 0 ? (
          <p className="py-4 text-center text-xs text-muted-foreground">{emptyText}</p>
        ) : (
          <ul className="flex flex-col gap-1.5">
            {safeRows.map((row) => {
              const pctOfMax = Math.max(2, (row.count / maxVal) * 100);
              const sharePct = ((row.count / total) * 100).toFixed(1);
              const href = getItemHref?.(row.key);
              const labelContent = formatKey ? formatKey(row.key) : row.key || "(unknown)";

              const inner = (
                <div className="relative flex min-w-0 items-center justify-between gap-3 overflow-hidden rounded-md px-2.5 py-1.5 text-xs">
                  <span
                    aria-hidden="true"
                    className="pointer-events-none absolute inset-y-0 left-0 rounded-md bg-primary/8 dark:bg-primary/14"
                    style={{ width: `${pctOfMax}%` }}
                  />
                  <span className="relative min-w-0 truncate font-medium text-foreground">
                    {labelContent}
                  </span>
                  <span className="relative shrink-0 text-right text-muted-foreground tabular-nums">
                    <strong className="font-medium text-foreground">{row.count.toLocaleString()}</strong>
                    <span className="ml-1.5 text-[0.6875rem]">{sharePct}%</span>
                  </span>
                </div>
              );

              return (
                <li key={row.key}>
                  {href ? (
                    <Link
                      to={href}
                      className="block rounded-md outline-none hover:ring-1 hover:ring-border focus-visible:ring-2 focus-visible:ring-ring"
                    >
                      {inner}
                    </Link>
                  ) : (
                    inner
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </CardContent>
      {footerNote ? (
        <CardFooter className="border-t border-hairline pt-3 text-[0.6875rem] text-muted-foreground">
          {footerNote}
        </CardFooter>
      ) : null}
    </Card>
  );
}
