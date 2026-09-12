import type { CSSProperties, ReactNode } from "react";
import { Activity, Boxes, CloudCog, Database, HardDrive, LayoutDashboard, Network, Package, ScrollText, ServerCog, Settings, WandSparkles } from "lucide-react";
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
    <section className="console-page-header" aria-labelledby="page-title">
      <div>
        <div className="console-page-heading">{Icon ? <Icon aria-hidden="true" /> : null}<h1 id="page-title">{meta.title}</h1></div>
        {meta.description ? <p className="console-page-description">{meta.description}</p> : null}
      </div>
      {meta.action || meta.score ? <div className="console-page-actions">
        {meta.score ? <div className="flex items-center gap-3" aria-label={meta.score.label}>
          <div className="score-ring flex size-14 items-center justify-center rounded-full text-lg font-semibold" style={{ "--score": `${meta.score.value}%` } as CSSProperties}><strong>{meta.score.value}</strong></div>
          <span className="flex flex-col text-sm">{meta.score.label}<b>{meta.score.status}</b></span>
        </div> : null}
        {meta.action}
      </div> : null}
      {meta.metrics.length ? <dl className="console-page-metrics" aria-label={meta.title}>
        {meta.metrics.map(metric => <div key={metric.label}><dt>{metric.label}</dt><dd>{metric.value}</dd></div>)}
      </dl> : null}
    </section>
  );
}
