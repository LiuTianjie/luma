import type { TaskEvent } from "./types";

export interface ExecutionAttempt {
  id: string;
  traceId: string;
  taskId: string;
  taskName: string;
  retry?: number;
  timestamp: number;
  frameworkOutcome: string;
  businessOutcome?: string;
  businessStatus?: string;
  outcome: string;
  duration?: number;
  businessKind?: string;
  businessId?: string;
  error?: TaskEvent;
  events: TaskEvent[];
}

export interface ExecutionGroup {
  id: string;
  attempts: ExecutionAttempt[];
  latest: ExecutionAttempt;
  outcome: string;
  observedAttempts: number;
}

export interface ExecutionPhase {
  name: string;
  started?: number;
  finished?: number;
  durationMs?: number;
  count?: number;
  outcome: string;
  businessId?: string;
}

const timestamp = (event: TaskEvent) => Number(event.timestamp) || 0;

// 同一事件可同时来自任务查询和关联查询，翻页时合并身份字段并保留时间顺序。
export function mergeEvents(
  previous: TaskEvent[],
  incoming: TaskEvent[],
): TaskEvent[] {
  const rows = new Map<string, TaskEvent>();
  for (const event of [...previous, ...incoming]) {
    const key = JSON.stringify([
      event.trace_id,
      event.span_id,
      event.event,
      event.timestamp,
      event.stage,
      event.business_kind,
      event.business_id,
    ]);
    rows.set(key, { ...rows.get(key), ...event });
  }
  return [...rows.values()].sort((a, b) => timestamp(b) - timestamp(a));
}

// 用任务编号聚合重试，每次尝试仍使用独立 Trace；缺少结束记录时保留未知。
export function executions(events: TaskEvent[]): ExecutionGroup[] {
  const attempts = new Map<string, ExecutionAttempt>();
  const anchors = events.filter(e => ["task_started", "task_finished"].includes(e.event));
  const identity = (e: TaskEvent) => `${e.trace_id}:${e.span_id || e.task_id || e.task_name || ""}`;
  for (const event of [...events].sort((a, b) => timestamp(a) - timestamp(b))) {
    if (!event.trace_id || event.event === "task_published") continue;
    let attemptId = identity(event);
    if (!["task_started", "task_finished"].includes(event.event) && !anchors.some(e => identity(e) === attemptId)) {
      const related = anchors.filter(e => e.trace_id === event.trace_id && e.task_id === event.task_id && e.task_name === event.task_name);
      const ids = [...new Set(related.map(identity))];
      if (ids.length === 1) attemptId = ids[0]!;
    }
    let attempt = attempts.get(attemptId);
    if (!attempt) {
      attempt = {
        id: attemptId,
        traceId: event.trace_id,
        taskId: "",
        taskName: "",
        timestamp: timestamp(event),
        frameworkOutcome: "unknown",
        outcome: "unknown",
        events: [],
      };
      attempts.set(attemptId, attempt);
    }
    attempt.events.push(event);
    attempt.taskId = event.task_id || attempt.taskId;
    attempt.taskName = event.task_name || attempt.taskName;
    attempt.businessId = event.business_id || attempt.businessId;
    attempt.businessKind = event.business_kind || attempt.businessKind;
    if (event.event === "task_finished") attempt.businessOutcome = event.business_outcome;
    else if (!attempt.events.some(e => e.event === "task_finished"))
      attempt.businessOutcome = event.business_outcome || attempt.businessOutcome;
    attempt.businessStatus = event.business_status || attempt.businessStatus;
    if (event.retry != null) attempt.retry = Number(event.retry);
    attempt.timestamp = Math.max(attempt.timestamp, timestamp(event));
    if (event.event === "task_finished") {
      attempt.frameworkOutcome = event.outcome || "unknown";
      attempt.duration = event.elapsed_seconds;
    }
    if (event.event === "task_failure" || (event.error_type && !attempt.error))
      attempt.error = event;
  }
  const groups = new Map<string, ExecutionAttempt[]>();
  for (const attempt of attempts.values()) {
    attempt.outcome = attempt.businessOutcome === "failed" || attempt.frameworkOutcome === "failed"
      ? "failed"
      : attempt.frameworkOutcome === "retrying" || attempt.businessOutcome === "retrying"
        ? "retrying"
        : attempt.frameworkOutcome === "cancelled" || attempt.businessOutcome === "cancelled"
          ? "cancelled"
          : attempt.frameworkOutcome === "succeeded" && (!attempt.businessOutcome || attempt.businessOutcome === "succeeded")
            ? "succeeded" : "unknown";
    const id = attempt.taskId ? `${attempt.taskName}:${attempt.taskId}` : attempt.id;
    const group = groups.get(id) || [];
    group.push(attempt);
    groups.set(id, group);
  }
  return [...groups.entries()]
    .map(([id, rows]) => {
      rows.sort(
        (a, b) => a.timestamp - b.timestamp || (a.retry ?? 0) - (b.retry ?? 0),
      );
      const latest = rows[rows.length - 1]!;
      const recovered =
        latest.outcome === "succeeded" &&
        ((latest.retry ?? 0) > 0 ||
          rows
            .slice(0, -1)
            .some((row) => ["failed", "retrying"].includes(row.outcome)));
      return {
        id,
        attempts: rows,
        latest,
        outcome: recovered ? "recovered" : latest.outcome,
        observedAttempts: rows.length,
      };
    })
    .sort((a, b) => b.latest.timestamp - a.latest.timestamp);
}

// 重复阶段展示累计工作时间；只有开始日志的阶段保留为最近记录。
export function executionPhases(events: TaskEvent[]): ExecutionPhase[] {
  const phases = new Map<string, ExecutionPhase>();
  for (const event of [...events].sort((a, b) => timestamp(a) - timestamp(b))) {
    if (
      !event.stage ||
      !["phase_started", "phase_finished"].includes(event.event)
    )
      continue;
    const phase = phases.get(event.stage) || {
      name: event.stage,
      outcome: "unknown",
    };
    phase.started = event.phase_started_at ?? phase.started;
    phase.businessId = event.business_id || phase.businessId;
    if (event.event === "phase_finished") {
      phase.finished = event.phase_finished_at;
      phase.durationMs = event.phase_duration_ms;
      phase.count = event.phase_count;
      phase.outcome = event.phase_count
        ? event.phase_outcome || "unknown"
        : "unknown";
    }
    phases.set(event.stage, phase);
  }
  return [...phases.values()].sort(
    (a, b) => (a.started || 0) - (b.started || 0),
  );
}
