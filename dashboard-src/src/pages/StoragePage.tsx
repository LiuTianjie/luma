import { StoragePanel } from "../components/StoragePanel";
import { t } from "../i18n";
import type { Lang } from "../types";
import type { DashboardViewModel } from "../dashboardViewModel";
import { PageHeader } from "./PageHeader";

export function StoragePage({ lang, vm }: { lang: Lang; vm: DashboardViewModel }) {
  const zh = lang === "zh";
  return (
    <>
      <PageHeader
        meta={{
          eyebrow: zh ? "存储状态" : "Storage",
          title: zh ? "存储类、卷与绑定关系" : "Storage classes, volumes, and bindings",
          description: zh
            ? "集中查看存储类、卷来源、节点绑定以及消费服务。"
            : "Review classes, volume sources, node bindings, and consuming services in one place.",
          metrics: [
            { label: "storageClass", value: vm.storageClasses.length },
            { label: t(lang, "volume"), value: vm.storageVolumes.length },
            { label: "Warnings", value: vm.storageWarnings.length },
          ],
        }}
      />
      <StoragePanel lang={lang} volumes={vm.storageVolumes} storageClasses={vm.storageClasses} warnings={vm.storageWarnings} />
    </>
  );
}
