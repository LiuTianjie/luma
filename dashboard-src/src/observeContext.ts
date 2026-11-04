/** Shared investigation state. Unknown URL keys are retained by navigation. */
export const OBSERVE_KEYS = [
  "observeHours",
  "observe_since",
  "observe_until",
  "observe_environment",
  "observe_service",
  "observe_instance",
  "observe_keyword",
  "observeTrace",
  "observeSpan",
  "observe_task_id",
  "observe_task_name",
  "observe_queue",
  "observe_outcome",
  "observe_attribute",
  "observe_value",
  "observe_min_duration_ms",
  "observeView",
  "logView",
  "service",
  "allocation",
];
export function observationQuery(
  search: string,
  now = Math.floor(Date.now() / 1000),
) {
  const p = new URLSearchParams(search);
  const hours = [1, 6, 24, 72].includes(Number(p.get("observeHours")))
    ? Number(p.get("observeHours"))
    : 1;
  const end = Number(p.get("observe_until"));
  const until = end > 0 && Number.isFinite(end) ? Math.min(end, now) : now;
  const start = Number(p.get("observe_since"));
  const since =
    start > 0 && start < until && until - start <= 259200
      ? start
      : until - hours * 3600;
  const query: Record<string, string | number> = {
    hours: Math.max(1, Math.ceil((until - since) / 3600)),
    since,
    until,
  };
  for (const key of ["environment", "service", "instance", "keyword"]) {
    const value = p.get(`observe_${key}`);
    if (value) query[key] = value;
  }
  return query;
}
export function investigationPath(
  path: string,
  search: string,
  changes: Record<string, string> = {},
) {
  const original = new URLSearchParams(search),
    next = new URLSearchParams(path.split("?")[1] || "");
  for (const key of OBSERVE_KEYS)
    if (original.has(key)) next.set(key, original.get(key)!);
  for (const [key, value] of Object.entries(changes))
    value ? next.set(key, value) : next.delete(key);
  return path.split("?")[0] + (next.size ? `?${next}` : "");
}
export function grafanaRange(search: string) {
  const q = observationQuery(search),
    params = new URLSearchParams(search);
  return params.has("observe_until") || params.has("observe_since")
    ? {
        from: String(Number(q.since) * 1000),
        to: String(Number(q.until) * 1000),
      }
    : { from: `now-${q.hours}h`, to: "now" };
}
export function logsExpression(app: string, search: string) {
  const q = observationQuery(search),
    p = new URLSearchParams(search);
  const filters = [app ? `app:=${JSON.stringify(app)}` : 'app:~".+"'];
  for (const [key, field] of [
    ["service", "service_name"],
    ["instance", "service_instance_id"],
    ["environment", "environment"],
  ])
    if (q[key]) filters.push(`${field}:=${JSON.stringify(q[key])}`);
  if (q.keyword) filters.push(`_msg:${JSON.stringify(q.keyword)}`);
  const trace = p.get("observeTrace");
  if (trace && /^[0-9a-f]{32}$/i.test(trace))
    filters.push(
      `(trace_id:=${JSON.stringify(trace.toLowerCase())} OR _msg:${JSON.stringify(trace.toLowerCase())})`,
    );
  const task = p.get("observe_task_id");
  if (task)
    filters.push(
      `(task_id:=${JSON.stringify(task)} OR _msg:${JSON.stringify(task)})`,
    );
  return filters.join(" ");
}
