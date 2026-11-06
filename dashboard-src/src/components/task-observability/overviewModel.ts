import type { MetricSource } from "./types";

export interface TaskMetric {
  key: string;
  name: string;
  runtime: string;
  queue: string;
  active?: number;
  oldest?: number;
  no_progress?: number;
  duration?: number;
  wait?: number;
  last?: number;
  results: Record<string, number>;
}

export const sourceReadable = (source?: MetricSource) =>
  source?.state === "ok" || source?.state === "empty";

export function overviewSources(sources: Record<string, MetricSource>) {
  const entries = Object.entries(sources);
  return {
    unavailable: entries.filter(([, s]) => !sourceReadable(s) && s.state !== "stale"),
    stale: entries.filter(([, s]) => s.state === "stale"),
    empty: entries.filter(([, s]) => s.state === "empty"),
  };
}

const outcomes = ["succeeded", "failed", "retrying", "cancelled", "unknown"];

// A successful log query with no matches is zero observed records. A failed
// query or a missing metric series must never turn into a measured zero.
export function completionCounts(sources: Record<string, MetricSource>) {
  const results = [sources.executions, sources.span_executions].filter(
    (source): source is MetricSource => source != null,
  );
  if (!results.length || results.some((s) => !sourceReadable(s))) return undefined;
  if (results.some((s) => s.state === "ok" && !s.rows.length && !s.counts)) return undefined;
  const counts: Record<string, number> = Object.fromEntries(outcomes.map((o) => [o, 0]));
  for (const source of results) {
    const values = source.counts
      ? Object.entries(source.counts)
      : source.rows.map((row) => [row.metric.outcome || row.metric.task_outcome || "unknown", Number(row.value[1])] as const);
    for (const [outcome, value] of values) {
      if (!Number.isFinite(value) || value < 0) return undefined;
      counts[outcome] = (counts[outcome] || 0) + value;
    }
  }
  return counts;
}

export function overviewProgress(sources: Record<string, MetricSource>, tasks: TaskMetric[]) {
  const active = tasks.filter((task) => task.active != null);
  const running = active.filter((task) => task.active! > 0);
  const longest = (field: "oldest" | "no_progress") =>
    running.filter((task) => task[field] != null)
      .sort((a, b) => b[field]! - a[field]!)[0];
  const last = sources.last_finished?.state === "ok"
    ? sources.last_finished.rows.map((row) => Number(row.value[1]))
      .filter((value) => Number.isFinite(value) && value > 0)
    : [];
  return {
    active: sources.active?.state === "ok" && active.length
      ? active.reduce((sum, task) => sum + task.active!, 0) : undefined,
    activeTypes: running.length,
    oldest: longest("oldest"),
    noProgress: longest("no_progress"),
    last: last.length ? Math.max(...last) : undefined,
    counts: completionCounts(sources),
  };
}

export function taskMetrics(
  sources: Record<string, MetricSource>,
): TaskMetric[] {
  const tasks = new Map<string, TaskMetric>();
  for (const [source, result] of Object.entries(sources)) {
    if (
      result.state !== "ok" ||
      ["last_finished", "processes", "drops"].includes(source)
    )
      continue;
    for (const row of result.rows) {
      const name = row.metric.task_name;
      const value = Number(row.value[1]);
      if (!name || !Number.isFinite(value) || value < 0) continue;
      const runtime =
        row.metric.task_framework ||
        (source.startsWith("span_") ? "celery" : "taskiq");
      const queue = row.metric.task_queue || "";
      const key = JSON.stringify([name, runtime, queue]);
      const item = tasks.get(key) || { key, name, runtime, queue, results: {} };
      if (source === "executions" || source === "span_executions")
        item.results[
          row.metric.outcome || row.metric.task_outcome || "unknown"
        ] = value;
      else if (source === "span_duration") item.duration = value;
      else if (
        ["active", "oldest", "no_progress", "duration", "wait"].includes(source)
      )
        item[source as "active"] = value;
      tasks.set(key, item);
    }
  }
  for (const row of sources.last_finished?.state === "ok"
    ? sources.last_finished.rows
    : []) {
    const value = Number(row.value[1]);
    if (!Number.isFinite(value) || value <= 0) continue;
    for (const item of tasks.values())
      if (
        item.name === row.metric.task_name &&
        item.runtime === row.metric.task_framework
      )
        item.last = Math.max(item.last || 0, value);
  }
  if (sources.executions?.source === "completion_logs" && sourceReadable(sources.executions))
    for (const task of tasks.values())
      for (const outcome of outcomes) task.results[outcome] ??= 0;
  return [...tasks.values()].sort(
    (a, b) =>
      Number((b.active || 0) > 0) - Number((a.active || 0) > 0) ||
      ((b.active || 0) > 0 ? b.no_progress ?? -1 : -1) - ((a.active || 0) > 0 ? a.no_progress ?? -1 : -1) ||
      ((b.active || 0) > 0 ? b.oldest ?? -1 : -1) - ((a.active || 0) > 0 ? a.oldest ?? -1 : -1) ||
      (b.results.failed || 0) - (a.results.failed || 0) ||
      (b.active || 0) - (a.active || 0) ||
      a.name.localeCompare(b.name),
  );
}

export function processMetrics(
  sources: Record<string, MetricSource>,
  now: number,
) {
  const processes = new Map<
    string,
    {
      id: string;
      service: string;
      role: string;
      queue: string;
      heartbeat?: number;
      started?: number;
    }
  >();
  if (sources.processes?.state !== "ok") return [];
  for (const row of sources.processes.rows) {
    const id = row.metric.service_instance_id;
    const value = Number(row.value[1]);
    if (!id || !Number.isFinite(value)) continue;
    const item = processes.get(id) || {
      id,
      service: row.metric.service_name,
      role: row.metric.task_role || "",
      queue: row.metric.task_queue || "",
    };
    if (row.metric.__name__ === "worker_heartbeat_seconds")
      item.heartbeat = value;
    else item.started = value;
    processes.set(id, item);
  }
  return [...processes.values()].map((item) => ({
    ...item,
    age: item.heartbeat ? Math.max(0, now - item.heartbeat) : undefined,
  }));
}
