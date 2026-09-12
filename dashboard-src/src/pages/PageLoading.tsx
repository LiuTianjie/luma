import { Skeleton } from "@/components/ui/skeleton";
import type { Lang } from "../types";

export function PageLoading({ lang = "zh" }: { lang?: Lang }) {
  const label = lang === "zh" ? "页面加载中…" : "Loading page…";
  return (
    <section className="flex flex-col gap-6" role="status" aria-busy="true" aria-live="polite">
      <div className="flex flex-col gap-6" aria-hidden="true">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="flex min-w-0 flex-col gap-3">
            <Skeleton className="h-9 w-56 max-w-full" />
            <Skeleton className="h-4 w-80 max-w-full" />
          </div>
          <Skeleton className="h-9 w-24" />
          <div className="flex w-full flex-wrap gap-2">
            <Skeleton className="h-6 w-24" />
            <Skeleton className="h-6 w-24" />
            <Skeleton className="h-6 w-24" />
          </div>
        </div>
        <Skeleton className="h-64 w-full" />
      </div>
      <p className="sr-only">{label}</p>
    </section>
  );
}
