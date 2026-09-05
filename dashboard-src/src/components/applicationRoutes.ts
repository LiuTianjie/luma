export const APPLICATION_TABS = [
  { id: "overview", zh: "概览", en: "Overview" },
  { id: "services", zh: "服务与实例", en: "Services" },
  { id: "logs", zh: "日志", en: "Logs" },
  { id: "metrics", zh: "指标", en: "Metrics" },
  { id: "config", zh: "配置", en: "Configuration" },
  { id: "versions", zh: "版本与回滚", en: "Versions" },
] as const;
export type ApplicationTab = typeof APPLICATION_TABS[number]["id"];
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
