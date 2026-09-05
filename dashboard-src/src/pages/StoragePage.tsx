import { InfrastructureNavigation } from "../components/InfrastructureNavigation";
import { StorageGovernancePanel } from "../components/StorageGovernancePanel";
import { toHref, useRouter } from "../router";
import { StoragePanel } from "../components/StoragePanel";
import { t } from "../i18n";
import type { Lang } from "../types";
import type { DashboardViewModel } from "../dashboardViewModel";
import { PageHeader } from "./PageHeader";
import "./InfrastructureWorkspace.css";

export function StoragePage({ lang, vm, token }: { lang: Lang; vm: DashboardViewModel; token: string }) {
  const zh = lang === "zh";
  const { path, navigate } = useRouter();
  const governance = path.startsWith("/storage/governance");
  return (
    <div className="infrastructure-workspace">
      <InfrastructureNavigation lang={lang} />
      <PageHeader
        meta={{
          eyebrow: zh ? "存储状态" : "Storage",
          title: governance ? (zh ? "容量与回收" : "Capacity and cleanup") : (zh ? "卷与存储类" : "Volumes and storage classes"),
          description: governance ? (zh ? "查看数据占用、保留策略与安全回收任务。" : "Inspect data usage, retention policies, and safe cleanup tasks.") : zh
            ? "集中查看存储类、卷来源、节点绑定以及消费服务。"
            : "Review classes, volume sources, node bindings, and consuming services in one place.",
          metrics: governance ? [] : [
            { label: "storageClass", value: vm.storageClasses.length },
            { label: t(lang, "volume"), value: vm.storageVolumes.length },
            { label: "Warnings", value: vm.storageWarnings.length },
          ],
        }}
      />
      <nav className="workspace-tabs" aria-label={zh ? "存储管理" : "Storage management"}>
        {[["/storage", zh ? "卷与存储类" : "Volumes and classes"], ["/storage/governance", zh ? "容量与回收" : "Capacity and cleanup"]].map(([href, label], index) => <a key={href} href={toHref(href)} className={governance === Boolean(index) ? "active" : ""} aria-current={governance === Boolean(index) ? "page" : undefined} onClick={(event) => {
          if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
          event.preventDefault(); navigate(href);
        }}>{label}</a>)}
      </nav>
      {governance ? <StorageGovernancePanel lang={lang} token={token} /> : <StoragePanel lang={lang} volumes={vm.storageVolumes} storageClasses={vm.storageClasses} warnings={vm.storageWarnings} />}
    </div>
  );
}
