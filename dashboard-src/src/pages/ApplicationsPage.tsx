import { useEffect } from "react";
import { applicationPath, parseApplicationPath } from "../components/applicationRoutes";
import { Plus } from "lucide-react";
import { ApplicationManagementPanel, type ApplicationUpdateRequest } from "../components/ApplicationManagementPanel";
import { groupApplications } from "../components/applicationModel";
import { t } from "../i18n";
import type { DashboardPayload, DashboardService, Lang } from "../types";
import { PageHeader } from "./PageHeader";
import { useRouter, useSearchParams } from "../router";

export function ApplicationsPage({
  lang,
  token,
  payload,
  onRefresh,
  onCreateApplication,
  onUpdateApplication,
  onNavigateToDeployments,
  onServiceTerminal,
}: {
  lang: Lang;
  token: string;
  payload: DashboardPayload;
  onRefresh: () => Promise<void> | void;
  onCreateApplication: () => void;
  onUpdateApplication: (request: ApplicationUpdateRequest) => void;
  onNavigateToDeployments?: () => void;
  onServiceTerminal?: (service: DashboardService, stack: string) => void;
}) {
  const searchParams = useSearchParams();
  const { path, navigate } = useRouter();
  const legacySelect = searchParams.get("select");
  const selectApp = parseApplicationPath(path).stack || legacySelect;
  useEffect(() => {
    if (legacySelect && !parseApplicationPath(path).stack) navigate(applicationPath(legacySelect), { replace: true });
  }, [legacySelect, path, navigate]);
  const zh = lang === "zh";
  const applications = groupApplications(payload.services || []);
  const healthy = applications.filter((app) => app.status === "healthy" || app.status === "running").length;
  const degraded = applications.filter((app) => app.status === "degraded" || app.status === "pending").length;
  const failed = applications.filter((app) => app.status === "failed").length;
  return (
    <>
      {!selectApp ? <PageHeader
        meta={{
          eyebrow: zh ? "应用管理" : "Applications",
          title: zh ? "应用" : "Applications",
          description: zh
            ? "管理应用、服务实例与发布版本。"
            : "Manage applications, service instances, and releases.",
          metrics: [
            { label: t(lang, "applications"), value: applications.length },
            { label: zh ? "健康" : "Healthy", value: healthy },
            { label: zh ? "降级" : "Degraded", value: degraded },
            { label: zh ? "失败" : "Failed", value: failed },
          ],
          action: (
            <button type="button" className="primary page-toolbar-cta" onClick={onCreateApplication}>
              <Plus size={16} aria-hidden="true" />
              {t(lang, "createApplication")}
            </button>
          ),
        }}
      /> : null}
      <ApplicationManagementPanel
        lang={lang}
        token={token}
        payload={payload}
        onRefresh={onRefresh}
        onUpdateApplication={onUpdateApplication}
        onNavigateToDeployments={onNavigateToDeployments}
        onServiceTerminal={onServiceTerminal}
        selectedStack={selectApp}
        onSelectApplication={(stack) => {
          navigate(stack ? applicationPath(stack) : "/apps");
        }}
      />
    </>
  );
}
