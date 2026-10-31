import { apiGet, apiPost, authHeaders, readJson } from "./apiClient";

export type AlertTab = "alerts" | "rules" | "notifications";
export type AlertPreset = { metric: string; name: string; description: string; threshold: number; forSeconds: number; unit: string };
export type LogQuery = { windowSeconds: number; task?: string; contains?: string; exclude?: string; field?: string; equals?: string };
export type AlertRule = { logQuery?: LogQuery; id: string; name: string; metric: string; target: string; threshold: number; forSeconds: number; severity: "warning" | "critical"; channelIds: string[]; enabled: boolean; repeatSeconds: number; noData: "keep" | "alert"; silencedUntil?: number };
export type AlertChannel = { id: string; name: string; type: "feishu"; enabled: boolean; appId: string; chatId: string; appSecretConfigured: boolean };
export type BuildAlertDetails = { buildId?: string; service?: string; image?: string; images?: Record<string, string>; imageRepository?: string; buildNode?: string; repository?: string; ref?: string; stage?: string; reason?: string; createdAt?: number; failedAt?: number; completedAt?: number };
export type LogAlertSample = { _msg: string; _time?: string; task?: string; alloc?: string; stream?: string };
export type LogAlertDetails = { app: string; task?: string; windowSeconds: number; windowStart: number; windowEnd: number; sampledAt: number; matchCount: number; total?: number; samples?: LogAlertSample[]; summary?: string; query?: Record<string, string>; historicalBackfill?: boolean };
export function alertLogExploreUrl(details: LogAlertDetails): string {
  const fields = [`app=${JSON.stringify(details.app)}`, ...(details.task ? [`task=${JSON.stringify(details.task)}`] : [])];
  const pane = { logs: { datasource: "victorialogs", queries: [{ refId: "A", datasource: { type: "victoriametrics-logs-datasource", uid: "victorialogs" }, queryType: "instant", expr: `{${fields.join(",")}}`, maxLines: 100 }], range: { from: new Date(details.windowStart * 1000).toISOString(), to: new Date(details.windowEnd * 1000).toISOString() } } };
  return `/grafana/explore?orgId=1&schemaVersion=1&panes=${encodeURIComponent(JSON.stringify(pane))}`;
}
export type AlertIncident = { id: number; ruleId: string; ruleName: string; target: string; metric: string; severity: string; status: string; value?: number | null; startedAt: number; firedAt?: number; resolvedAt?: number; updatedAt?: number; acknowledgedAt?: number; noData?: boolean; buildDetails?: BuildAlertDetails; application?: string; logDetails?: LogAlertDetails };
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
  return { id: "", name: preset?.name || "", metric: preset?.metric || "", target: preset?.metric === "app.log_matches" ? "" : "*", ...(preset?.metric === "app.log_matches" ? { logQuery: { windowSeconds: 300 } } : {}), threshold: preset?.threshold ?? 90, forSeconds: preset?.forSeconds ?? 300, severity: "warning", channelIds: [], enabled: true, repeatSeconds: 3600, noData: "keep" };
}
export function ruleRequest(rule: AlertRule) {
  if (!rule.name.trim() || !rule.metric || !rule.target.trim()) throw new Error("Name, metric and target are required / 请填写名称、指标和对象");
  if (![rule.threshold, rule.forSeconds, rule.repeatSeconds].every(Number.isFinite) || rule.threshold < 0 || rule.forSeconds < 0 || rule.repeatSeconds < 60) throw new Error("Invalid threshold or duration / 阈值或持续时间无效");
  if (rule.metric === "app.log_matches") {
    const q = rule.logQuery;
    if (rule.target.trim() === "*" || !q || !Number.isInteger(q.windowSeconds) || q.windowSeconds < 60 || q.windowSeconds > 900)
      throw new Error("Select one application and a 60–900 second window / 请选择单个应用及 60–900 秒窗口");
    if ((!q.contains && !(q.field && q.equals)) || Boolean(q.field) !== Boolean(q.equals))
      throw new Error("Provide matching text or a JSON field and value / 请填写匹配文本或 JSON 字段和值");
  }
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
  const labels: Record<string, [string, string]> = { pending: ["等待中", "Pending"], sending: ["发送中", "Sending"], retry: ["等待重试", "Retrying"], firing: ["触发中", "Firing"], resolved: ["已恢复", "Resolved"], closed: ["已关闭", "Closed"], sent: ["已发送", "Sent"], failed: ["发送失败", "Failed"], retrying: ["等待重试", "Retrying"], suppressed: ["已抑制", "Suppressed"], cancelled: ["已取消", "Cancelled"] };
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
    "build.failed": ["Latest build failed", "A new failed build starts a separate incident; a later success resolves it, or it can be closed manually"],
    "app.log_matches": ["Service log matches", "Matching log lines for one application / task, aggregated by observe"],
    "app.http_p95": ["App p95 latency", "Traefik ingress p95 from luma-observe"],
    "app.http_5xx_ratio": ["App 5xx ratio", "Public HTTP 5xx / requests from luma-observe"],
    "app.nomad_not_running": ["App has no running instances", "An application that should run has no running allocation"],
    "app.nomad_failed": ["App failed allocations", "Failed allocations Nomad still wants to run"],
  };
  const translation = labels[preset.metric];
  const units: Record<string, [string, string]> = { seconds: ["秒", "s"], percent: ["%", "%"], count: ["计数", "count"], ratio: ["占比", "ratio"] };
  return { ...preset, ...(!zh && translation ? { name: translation[0], description: translation[1] } : {}), unit: units[preset.unit]?.[zh ? 0 : 1] || preset.unit };
}

export function alertEventLabel(kind: string, zh: boolean): string {
  const labels: Record<string, [string, string]> = { test: ["渠道测试", "Channel test"], firing: ["告警触发", "Alert fired"], resolved: ["恢复", "Recovered"], repeat: ["重复提醒", "Repeat reminder"], pending: ["等待持续条件", "Pending duration"], acknowledged: ["停止提醒", "Reminders stopped"], manual_closed: ["手动关闭", "Closed by operator"], build_superseded: ["新失败已接替", "Superseded by a new failure"], notification_silenced: ["通知已静默", "Notification silenced"], rule_deleted: ["规则删除", "Rule deleted"], rule_changed: ["规则变更", "Rule changed"], rule_disabled: ["规则停用", "Rule disabled"] };
  return labels[kind]?.[zh ? 0 : 1] || kind;
}

// Each tab has independent data needs; hidden tables must not delay its load.
export async function fetchAlertTab(tab: AlertTab, token: string, filter: string, signal?: AbortSignal, application = "") {
  const appQuery = application ? `app=${encodeURIComponent(application)}` : "";
  const [overview, presets, rules, channels, incidents, deliveries] = await Promise.all([
    getAlerting<AlertOverview>(`overview${appQuery ? `?${appQuery}` : ""}`, token, signal),
    tab === "rules" ? getAlerting<AlertPage<AlertPreset>>("presets", token, signal) : undefined,
    tab === "rules" ? getAlerting<AlertPage<AlertRule>>(`rules${appQuery ? `?${appQuery}` : ""}`, token, signal) : undefined,
    tab !== "alerts" ? getAlerting<AlertPage<AlertChannel>>("channels", token, signal) : undefined,
    tab === "alerts" ? getAlerting<AlertPage<AlertIncident>>(`incidents?limit=50${appQuery ? `&${appQuery}` : ""}${filter ? `&status=${encodeURIComponent(filter)}` : ""}`, token, signal) : undefined,
    tab === "notifications" ? getAlerting<AlertPage<AlertDelivery>>("deliveries?limit=50", token, signal) : undefined,
  ]);
  return { overview, presets, rules, channels, incidents, deliveries };
}
