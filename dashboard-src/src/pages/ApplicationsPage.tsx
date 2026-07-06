import { Plus } from "lucide-react";
import { ApplicationManagementPanel, type ApplicationUpdateRequest } from "../components/ApplicationManagementPanel";
import { groupApplications } from "../components/applicationModel";
import { t } from "../i18n";
import type { DashboardPayload, Lang } from "../types";
import { PageHeader } from "./PageHeader";

export function ApplicationsPage({
  lang,
  token,
  payload,
  onRefresh,
  onCreateApplication,
  onUpdateApplication,
}: {
  lang: Lang;
  token: string;
  payload: DashboardPayload;
  onRefresh: () => Promise<void> | void;
  onCreateApplication: () => void;
  onUpdateApplication: (request: ApplicationUpdateRequest) => void;
}) {
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
            { label: zh ? "Healthy" : "Healthy", value: healthy },
            { label: zh ? "Degraded" : "Degraded", value: degraded },
            { label: zh ? "Failed" : "Failed", value: failed },
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
      />
    </>
  );
}
