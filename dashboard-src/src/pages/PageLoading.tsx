import { Skeleton } from "@/components/ui/skeleton";
import type { Lang } from "../types";

export function PageLoading({ lang = "zh" }: { lang?: Lang }) {
  const label = lang === "zh" ? "页面加载中…" : "Loading page…";
  return (
    <section className="flex flex-col gap-6" aria-busy="true" aria-live="polite">
      <div className="flex flex-col gap-3 lg:flex-row lg:justify-between">
        <div className="flex flex-col gap-2">
          <Skeleton className="h-3 w-24" />
          <Skeleton className="h-8 w-56" />
          <Skeleton className="h-4 w-80" />
        </div>
        <div className="flex gap-2">
          <Skeleton className="h-14 w-24" />
          <Skeleton className="h-14 w-24" />
          <Skeleton className="h-14 w-24" />
        </div>
      </div>
      <Skeleton className="h-64 w-full rounded-xl" />
      <p className="sr-only">{label}</p>
    </section>
  );
}
