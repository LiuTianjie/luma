import { ApplicationManagementPanel, type ApplicationUpdateRequest } from "../components/ApplicationManagementPanel";
import type { DashboardPayload, Lang } from "../types";

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
  return (
    <ApplicationManagementPanel
      lang={lang}
      token={token}
      payload={payload}
      onRefresh={onRefresh}
      onCreateApplication={onCreateApplication}
      onUpdateApplication={onUpdateApplication}
    />
  );
}
