import { useMemo, type ReactNode } from "react";
import { Link } from "react-router";
import {
  Activity,
  AlertTriangle,
  Globe,
  MousePointerClick,
  Navigation,
  Terminal,
} from "lucide-react";
import type { Breadcrumb } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty";
import { cn } from "@/lib/utils";
import { paths } from "@/router/paths";
import { formatDeltaBeforeCrash, formatShortTime } from "@/utils/format";
import { getBreadcrumbMeta } from "@/utils/geo";

interface BreadcrumbTrailProps {
  projectSlug: string;
  since?: string;
  breadcrumbs: Breadcrumb[];
  crashTimestamp?: string;
  filterValue?: string;
  onChangeFilter?: (next: string) => void;
  compact?: boolean;
}

function renderCategoryIcon(tone: string): ReactNode {
  const cls = "size-3.5 shrink-0";
  switch (tone) {
    case "click":
      return <MousePointerClick className={cls} aria-hidden="true" />;
    case "nav":
      return <Navigation className={cls} aria-hidden="true" />;
    case "http":
      return <Globe className={cls} aria-hidden="true" />;
    case "console":
      return <Terminal className={cls} aria-hidden="true" />;
    case "exception":
      return <AlertTriangle className={cn(cls, "text-destructive")} aria-hidden="true" />;
    default:
      return <Activity className={cls} aria-hidden="true" />;
  }
}

export function BreadcrumbTrail({
  projectSlug,
  since,
  breadcrumbs,
  crashTimestamp,
  filterValue = "all",
  onChangeFilter,
  compact = false,
}: BreadcrumbTrailProps) {
  const safeList = breadcrumbs || [];
  const counts = useMemo(() => {
    const map: Record<string, number> = { all: safeList.length };
    for (const bc of safeList) {
      const m = getBreadcrumbMeta(bc.category, bc.level);
      map[m.key] = (map[m.key] || 0) + 1;
    }
    return map;
  }, [safeList]);

  const filtered = useMemo(() => {
    if (filterValue === "all") return safeList;
    return safeList.filter((bc) => getBreadcrumbMeta(bc.category, bc.level).key === filterValue);
  }, [safeList, filterValue]);

  if (safeList.length === 0) {
    return (
      <Empty className="py-8">
        <EmptyHeader>
          <EmptyTitle>该异常样本未附带客户端操作面包屑</EmptyTitle>
          <EmptyDescription>
            使用 <code>@nickname4th/luma-eye</code> SDK 或内置 <code>/_e/eye.js</code> 时，会自动记录崩溃前最近 50 步点击、路由跳转、网络请求与控制台输出。
          </EmptyDescription>
        </EmptyHeader>
      </Empty>
    );
  }

  const filterOptions = [
    { key: "all", label: "全部步骤" },
    { key: "ui.click", label: "用户点击" },
    { key: "navigation", label: "路由跳转" },
    { key: "http", label: "网络请求" },
    { key: "console", label: "控制台" },
    { key: "lifecycle", label: "生命周期" },
    { key: "exception", label: "抛出异常" },
  ].filter((opt) => opt.key === "all" || (counts[opt.key] || 0) > 0);

  return (
    <div className="flex flex-col gap-3">
      {!compact && onChangeFilter ? (
        <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="面包屑类型筛选">
          {filterOptions.map((opt) => {
            const active = filterValue === opt.key;
            return (
              <Button
                key={opt.key}
                type="button"
                size="xs"
                variant={active ? "secondary" : "outline"}
                aria-pressed={active}
                onClick={() => onChangeFilter(opt.key)}
              >
                <span>{opt.label}</span>
                <span className="text-muted-foreground tabular-nums">{counts[opt.key] || 0}</span>
              </Button>
            );
          })}
        </div>
      ) : null}

      <ol className="relative flex flex-col divide-y divide-hairline rounded-lg border border-border bg-card">
        {filtered.map((bc, idx) => {
          const meta = getBreadcrumbMeta(bc.category, bc.level);
          const data = bc.data || {};
          const status = typeof data.status === "number" ? data.status : undefined;
          const durMs = typeof data.duration_ms === "number" ? data.duration_ms : undefined;
          const traceId = typeof data.trace_id === "string" ? data.trace_id : undefined;
          const fromRoute = typeof data.from === "string" ? data.from : undefined;
          const toRoute = typeof data.to === "string" ? data.to : undefined;
          const hasExtraData = Object.keys(data).length > 0;
          const isError =
            meta.tone === "exception" ||
            bc.level === "error" ||
            bc.level === "fatal" ||
            (status !== undefined && status >= 400);

          return (
            <li
              key={`${bc.ts || idx}-${idx}`}
              className={cn(
                "flex flex-col gap-1.5 px-4 py-3 text-xs",
                isError && "bg-destructive-soft/40",
              )}
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="inline-flex items-center gap-1.5 font-medium text-foreground">
                    {renderCategoryIcon(meta.tone)}
                    <span>{meta.label}</span>
                  </span>
                  {bc.level && bc.level !== "info" ? (
                    <Badge
                      variant={
                        bc.level === "error" || bc.level === "fatal"
                          ? "destructive"
                          : bc.level === "warn" || bc.level === "warning"
                            ? "warning"
                            : "outline"
                      }
                    >
                      {bc.level}
                    </Badge>
                  ) : null}
                  {status !== undefined ? (
                    <Badge variant={status >= 400 ? "destructive" : "outline"}>HTTP {status}</Badge>
                  ) : null}
                  {durMs !== undefined ? (
                    <span className="font-mono text-muted-foreground tabular-nums">{durMs}ms</span>
                  ) : null}
                </div>

                <div className="flex items-center gap-2 font-mono text-[0.6875rem] text-muted-foreground tabular-nums">
                  {crashTimestamp ? (
                    <span className={cn(meta.tone === "exception" && "font-semibold text-destructive-foreground")}>
                      {formatDeltaBeforeCrash(bc.ts, crashTimestamp)}
                    </span>
                  ) : null}
                  {bc.ts ? <span>{formatShortTime(bc.ts)}</span> : null}
                </div>
              </div>

              <div className="font-mono text-xs text-foreground wrap-anywhere">
                {bc.message || "(无描述)"}
              </div>

              {fromRoute || toRoute || traceId ? (
                <div className="flex flex-wrap items-center gap-2 text-[0.6875rem]">
                  {fromRoute && toRoute ? (
                    <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-muted-foreground">
                      {fromRoute} → {toRoute}
                    </span>
                  ) : null}
                  {traceId ? (
                    <Link
                      to={paths.projectSection(projectSlug, "logs", since, { trace: traceId })}
                      className="font-mono text-link hover:underline"
                    >
                      Trace {traceId.slice(0, 14)}… →
                    </Link>
                  ) : null}
                </div>
              ) : null}

              {hasExtraData && !compact ? (
                <details className="mt-0.5 text-[0.6875rem]">
                  <summary className="cursor-pointer text-muted-foreground select-none hover:text-foreground">
                    展开附加上下文 JSON
                  </summary>
                  <pre className="mt-1.5 overflow-x-auto rounded-md bg-muted p-2.5 font-mono text-[0.6875rem] text-foreground">
                    {JSON.stringify(data, null, 2)}
                  </pre>
                </details>
              ) : null}
            </li>
          );
        })}
      </ol>
    </div>
  );
}
