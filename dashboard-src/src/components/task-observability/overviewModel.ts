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
      if (!name || !Number.isFinite(value)) continue;
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
  return [...tasks.values()].sort(
    (a, b) =>
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
