import { ObservabilityPanel } from "../components/ObservabilityPanel";
import type { Lang } from "../types";
import type { DashboardViewModel } from "../dashboardViewModel";

export function ObservabilityPage({
  lang,
  token,
  vm,
}: {
  lang: Lang;
  token: string;
  vm: DashboardViewModel;
}) {
  return <ObservabilityPanel lang={lang} token={token} nodes={vm.nodes} services={vm.services} />;
}
