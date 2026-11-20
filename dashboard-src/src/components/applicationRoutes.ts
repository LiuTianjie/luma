import { hasAddonCapability, type DashboardAddonsSummary } from "../addonCapabilities.ts";

export const APPLICATION_TABS = [
  { id: "overview", zh: "概览", en: "Overview", requiresCapability: undefined },
  { id: "services", zh: "服务与实例", en: "Services", requiresCapability: undefined },
  { id: "alerts", zh: "告警", en: "Alerts", requiresCapability: undefined },
  { id: "logs", zh: "日志", en: "Logs", requiresCapability: undefined },
  { id: "metrics", zh: "指标", en: "Metrics", requiresCapability: undefined },
  { id: "dashboards", zh: "看板", en: "Dashboards", requiresCapability: "observe.dashboards" },
  { id: "eye", zh: "前端监控", en: "Frontend", requiresCapability: "eye.rum" },
  { id: "config", zh: "配置", en: "Configuration", requiresCapability: undefined },
  { id: "secrets", zh: "密钥", en: "Secrets", requiresCapability: undefined },
  { id: "versions", zh: "版本与回滚", en: "Versions", requiresCapability: undefined },
] as const;

export type ApplicationTab = typeof APPLICATION_TABS[number]["id"];

export function visibleApplicationTabs(
  addons?: DashboardAddonsSummary | null,
  options?: { eyeMatched?: boolean; activeTab?: ApplicationTab },
) {
  return APPLICATION_TABS.filter((item) => {
    if (!hasAddonCapability(addons, item.requiresCapability)) return false;
    if (item.id === "eye") {
      return Boolean(options?.eyeMatched || options?.activeTab === "eye");
    }
    return true;
  });
}

export function applicationPath(stack: string, tab: ApplicationTab = "overview", service?: string) {
  return `/apps/${encodeURIComponent(stack)}/${tab}${service ? `/${encodeURIComponent(service)}` : ""}`;
}
function decode(segment: string) {
  try { return decodeURIComponent(segment); } catch { return segment; }
}
export function parseApplicationPath(path: string): { stack: string | null; tab: ApplicationTab; service?: string } {
  const parts = path.split("/").filter(Boolean);
  const tab = APPLICATION_TABS.find((item) => item.id === parts[2])?.id || "overview";
  return { stack: parts[0] === "apps" && parts[1] ? decode(parts[1]) : null, tab, service: tab === "services" && parts[3] ? decode(parts[3]) : undefined };
}
