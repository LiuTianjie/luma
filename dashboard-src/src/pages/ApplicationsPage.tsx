import { Plus } from "lucide-react";
import { ApplicationManagementPanel, type ApplicationUpdateRequest } from "../components/ApplicationManagementPanel";
import { groupApplications } from "../components/applicationModel";
import { t } from "../i18n";
import type { DashboardPayload, Lang } from "../types";
import { PageHeader } from "./PageHeader";
import type { NavPage } from "../dashboardViewModel";
import { useSearchParams } from "../router";

export function ApplicationsPage({
  lang,
  token,
  payload,
  onRefresh,
  onCreateApplication,
  onUpdateApplication,
  onNavigateToDeployments,
}: {
  lang: Lang;
  token: string;
  payload: DashboardPayload;
  onRefresh: () => Promise<void> | void;
  onCreateApplication: () => void;
  onUpdateApplication: (request: ApplicationUpdateRequest) => void;
  onNavigateToDeployments?: () => void;
}) {
  const searchParams = useSearchParams();
  const selectApp = searchParams.get("select");
  const zh = lang === "zh";
  const applications = groupApplications(payload.services || []);
  const healthy = applications.filter((app) => app.status === "healthy" || app.status === "running").length;
  const degraded = applications.filter((app) => app.status === "degraded" || app.status === "pending").length;
  const failed = applications.filter((app) => app.status === "failed").length;
  return (
    <>
      <PageHeader
        meta={{
          eyebrow: zh ? "应用管理" : "Applications",
          title: zh ? "应用生命周期与运行态" : "Application lifecycle and runtime",
          description: zh
            ? "搜索、筛选、查看日志、读取部署配置、查看版本并执行受保护的运行态操作。"
            : "Search, filter, read logs, inspect deployment config, review versions, and run guarded runtime actions.",
          metrics: [
            { label: t(lang, "applications"), value: applications.length },
            { label: zh ? "健康" : "Healthy", value: healthy },
            { label: zh ? "降级" : "Degraded", value: degraded },
            { label: zh ? "失败" : "Failed", value: failed },
          ],
          action: (
            <button type="button" className="page-toolbar-cta" onClick={onCreateApplication}>
              <Plus size={16} aria-hidden="true" />
              {t(lang, "createApplication")}
            </button>
          ),
        }}
      />
      <ApplicationManagementPanel
        lang={lang}
        token={token}
        payload={payload}
        onRefresh={onRefresh}
        onUpdateApplication={onUpdateApplication}
        onNavigateToDeployments={onNavigateToDeployments}
        initialSelect={selectApp}
      />
    </>
  );
}
