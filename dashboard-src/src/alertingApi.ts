import { apiGet, apiPost, authHeaders, readJson } from "./apiClient";

export type AlertTab = "alerts" | "rules" | "notifications";
export type AlertPreset = { metric: string; name: string; description: string; threshold: number; forSeconds: number; unit: string };
export type AlertRule = { id: string; name: string; metric: string; target: string; threshold: number; forSeconds: number; severity: "warning" | "critical"; channelIds: string[]; enabled: boolean; repeatSeconds: number; noData: "keep" | "alert"; silencedUntil?: number };
export type AlertChannel = { id: string; name: string; type: "feishu"; enabled: boolean; appId: string; chatId: string; appSecretConfigured: boolean };
export type AlertIncident = { id: number; ruleId: string; ruleName: string; target: string; metric: string; severity: string; status: string; value?: number | null; startedAt: number; firedAt?: number; resolvedAt?: number; updatedAt?: number; acknowledgedAt?: number; noData?: boolean };
export type AlertDelivery = { id: number; channelId: string; incidentId?: number; kind: string; status: string; attempts: number; nextAttemptAt?: number; lastError?: string; createdAt: number; sentAt?: number };
export type AlertOverview = { counts: { pending: number; firing: number; resolved: number }; lastEvaluatedAt: number | null; silencedUntil: number; enabledRules: number; channels: number };
export type AlertEvent = { id: number; kind: string; at: number; detail: unknown };
export type AlertPage<T> = { items: T[]; nextCursor?: string | null };
const base = "/v1/alerting";
export const getAlerting = <T,>(path: string, token: string, signal?: AbortSignal) => apiGet<T>(`${base}/${path}`, token, signal);
export const postAlerting = <T,>(path: string, token: string, body: unknown, signal?: AbortSignal) => apiPost<T>(`${base}/${path}`, token, body, signal);
export async function deleteAlerting(path: string, token: string, signal?: AbortSignal) {
  return readJson(await fetch(`${base}/${path}`, { method: "DELETE", headers: authHeaders(token), signal }));
}

export function newAlertRule(preset?: AlertPreset): AlertRule {
  return { id: "", name: preset?.name || "", metric: preset?.metric || "", target: "*", threshold: preset?.threshold ?? 90, forSeconds: preset?.forSeconds ?? 300, severity: "warning", channelIds: [], enabled: true, repeatSeconds: 3600, noData: "keep" };
}
export function ruleRequest(rule: AlertRule) {
  if (!rule.name.trim() || !rule.metric || !rule.target.trim()) throw new Error("Name, metric and target are required / 请填写名称、指标和对象");
  if (![rule.threshold, rule.forSeconds, rule.repeatSeconds].every(Number.isFinite) || rule.threshold < 0 || rule.forSeconds < 0 || rule.repeatSeconds < 60) throw new Error("Invalid threshold or duration / 阈值或持续时间无效");
  return { ...rule, id: rule.id || undefined, name: rule.name.trim(), target: rule.target.trim(), channelIds: [...new Set(rule.channelIds)] };
}
export function channelRequest(id: string, name: string, enabled: boolean, appId: string, appSecret: string, chatId: string) {
  if (!appId.trim() || !chatId.trim()) throw new Error("App ID and chat ID are required / 请填写 App ID 和群聊 ID");
  if (!id && !appSecret.trim()) throw new Error("App Secret is required / 请填写 App Secret");
  return { id: id || undefined, name: name.trim() || "飞书告警", type: "feishu" as const, enabled, appId: appId.trim(), chatId: chatId.trim(), ...(appSecret.trim() ? { appSecret: appSecret.trim() } : {}) };
}

export function mergeAlertPages<T extends { id: string | number }>(previous: T[], next: T[]): T[] {
  const merged = new Map(previous.map((item) => [item.id, item]));
  next.forEach((item) => merged.set(item.id, item));
  return [...merged.values()];
}
export function alertStatusLabel(status: string, zh: boolean) {
  const labels: Record<string, [string, string]> = { pending: ["等待中", "Pending"], sending: ["发送中", "Sending"], retry: ["等待重试", "Retrying"], firing: ["触发中", "Firing"], resolved: ["已恢复", "Resolved"], closed: ["管理关闭", "Closed by configuration"], sent: ["已发送", "Sent"], failed: ["发送失败", "Failed"], retrying: ["等待重试", "Retrying"], suppressed: ["已抑制", "Suppressed"], cancelled: ["已取消", "Cancelled"] };
  return labels[status]?.[zh ? 0 : 1] || status;
}

export function localizeAlertPreset(preset: AlertPreset, zh: boolean): AlertPreset {
  const labels: Record<string, [string, string]> = {
    "node.offline": ["Node heartbeat missing", "Time since the last Agent heartbeat; independent of Nomad scheduling state"],
    "node.cpu": ["Sustained CPU usage", "Fresh Linux host CPU samples"],
    "node.memory": ["Sustained memory usage", "Host memory usage"],
    "node.disk": ["Disk space pressure", "Usage of the filesystem sampled by the Agent"],
    "node.inode": ["Inode pressure", "Inode usage of the filesystem sampled by the Agent"],
    "task.queue_age": ["Task queue delay", "Age of the oldest queued task, grouped by agent / builder / build"],
    "build.failed": ["Latest build failed", "The latest build per application failed; recovers after a successful build"],
  };
  const translation = labels[preset.metric];
  const units: Record<string, [string, string]> = { seconds: ["秒", "s"], percent: ["%", "%"], count: ["计数", "count"] };
  return { ...preset, ...(!zh && translation ? { name: translation[0], description: translation[1] } : {}), unit: units[preset.unit]?.[zh ? 0 : 1] || preset.unit };
}

export function alertEventLabel(kind: string, zh: boolean): string {
  const labels: Record<string, [string, string]> = { test: ["渠道测试", "Channel test"], firing: ["告警触发", "Alert fired"], resolved: ["恢复", "Recovered"], repeat: ["重复提醒", "Repeat reminder"], pending: ["等待持续条件", "Pending duration"], acknowledged: ["人工确认", "Acknowledged"], rule_deleted: ["规则删除", "Rule deleted"], rule_changed: ["规则变更", "Rule changed"], rule_disabled: ["规则停用", "Rule disabled"] };
  return labels[kind]?.[zh ? 0 : 1] || kind;
}
