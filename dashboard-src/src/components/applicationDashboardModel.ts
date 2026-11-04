import { grafanaRange, observationQuery } from "../observeContext.ts";
export type ApplicationDashboard = {
  uid: string;
  title: string;
};

// Grafana dashboard UIDs are limited to 40 letters, digits, hyphens or underscores.
const DASHBOARD_UID = /^[a-zA-Z0-9_-]{1,40}$/;

export function applicationDashboardTag(app: string): string {
  return `luma-app:${app}`;
}

export function applicationDashboardSearchPath(app: string): string {
  const params = new URLSearchParams({
    type: "dash-db",
    tag: applicationDashboardTag(app),
    limit: "100",
  });
  return `/grafana/api/search?${params.toString()}`;
}

export function readApplicationDashboards(payload: unknown, app: string): ApplicationDashboard[] {
  if (!Array.isArray(payload)) throw new Error("Invalid Grafana dashboard list");
  const tag = applicationDashboardTag(app);
  const dashboards = new Map<string, ApplicationDashboard>();
  for (const item of payload) {
    if (!item || typeof item !== "object" || item.type !== "dash-db") continue;
    if (!Array.isArray(item.tags) || !item.tags.includes(tag)) continue;
    if (typeof item.uid !== "string" || !DASHBOARD_UID.test(item.uid)) continue;
    if (typeof item.title !== "string" || !item.title.trim()) continue;
    if (!dashboards.has(item.uid)) {
      dashboards.set(item.uid, { uid: item.uid, title: item.title.trim() });
    }
  }
  return [...dashboards.values()].sort((a, b) => a.title.localeCompare(b.title) || a.uid.localeCompare(b.uid));
}

export function applicationDashboardUrl(uid: string, app: string, embedded = true, search = ""): string {
  if (!DASHBOARD_UID.test(uid)) throw new Error("Invalid Grafana dashboard UID");
  const params = new URLSearchParams({ orgId: "1", "var-app": app, ...grafanaRange(search) });
  const scope = observationQuery(search);
  for (const key of ["environment", "service", "instance"]) if (scope[key]) params.set(`var-${key}`,String(scope[key]));
  if (embedded) {
    params.set("kiosk", "tv");
    params.set("autofitpanels", "true");
  }
  // Ignore search-result URLs: all dashboards use the authenticated same-origin proxy.
  return `/grafana/d/${uid}?${params.toString()}`;
}
