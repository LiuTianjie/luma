import { Skeleton } from "@/components/ui/skeleton";
import type { Lang } from "../types";

export function PageLoading({ lang = "zh" }: { lang?: Lang }) {
  const label = lang === "zh" ? "页面加载中…" : "Loading page…";
  return (
    <section className="flex flex-col gap-6" aria-busy="true" aria-live="polite">
      <div className="console-page-header">
        <div className="flex min-w-0 flex-col gap-3">
          <Skeleton className="h-9 w-56 max-w-full" />
          <Skeleton className="h-4 w-80 max-w-full" />
        </div>
        <Skeleton className="h-9 w-24" />
        <div className="console-page-metrics"><Skeleton className="h-8 w-24 rounded-full" /><Skeleton className="h-8 w-24 rounded-full" /><Skeleton className="h-8 w-24 rounded-full" /></div>
      </div>
      <Skeleton className="h-64 w-full rounded-xl" />
      <p className="sr-only">{label}</p>
    </section>
  );
}
