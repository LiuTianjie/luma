import type { ReactNode } from "react";
import { Activity, Boxes, CloudCog, Database, HardDrive, LayoutDashboard, Network, Package, ScrollText, ServerCog, Settings, WandSparkles } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Progress, ProgressLabel, ProgressValue } from "@/components/ui/progress";
import { cn } from "@/lib/utils";
import { pageForPath } from "../routes";
import { useRouter } from "../router";
const pageIcons = { overview: LayoutDashboard, applications: Boxes, deployments: ScrollText, deploy: Package, builder: Package, nodes: ServerCog, storage: HardDrive, registry: Database, observability: Activity, credentials: Settings, setup: WandSparkles, lae: CloudCog, notfound: undefined };

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
  const { path } = useRouter();
  const Icon = path === "/fleet/network" ? Network : pageIcons[pageForPath(path)];
  return (
    <section className="flex min-w-0 flex-wrap items-start justify-between gap-4" aria-labelledby="page-title">
      <div className="flex min-w-0 flex-1 basis-64 flex-col gap-2">
        <div className="flex min-w-0 items-start gap-2">
          {Icon ? <Icon className="mt-1 size-5 shrink-0" aria-hidden="true" /> : null}
          <h1 id="page-title" className={cn("min-w-0 font-semibold tracking-tight wrap-anywhere", meta.variant === "ops" ? "text-xl" : "text-2xl")}>{meta.title}</h1>
        </div>
        {meta.description ? <p className="text-sm/relaxed text-muted-foreground wrap-anywhere">{meta.description}</p> : null}
      </div>
      {meta.action || meta.score ? <div className="flex max-w-full flex-wrap items-center gap-3">
        {meta.score ? <div className="flex w-48 flex-col gap-1">
          <Progress value={meta.score.value}>
            <ProgressLabel>{meta.score.label}</ProgressLabel>
            <ProgressValue />
          </Progress>
          <p className="text-sm text-muted-foreground">{meta.score.status}</p>
        </div> : null}
        {meta.action}
      </div> : null}
      {meta.metrics.length ? <div className="flex basis-full flex-wrap gap-2" aria-label={meta.title}>
        {meta.metrics.map(metric => <Badge key={metric.label} variant="outline"><span>{metric.label}</span><span className="tabular-nums">{metric.value}</span></Badge>)}
      </div> : null}
    </section>
  );
}
