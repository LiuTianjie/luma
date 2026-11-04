// Development-only synthetic records for the task board's interactive preview.
import type {
  TaskEvent,
  TraceSpan,
} from "./src/components/task-observability/types";

export function taskObservationFixture(view: string, params: URLSearchParams) {
  const now = Math.floor(Date.now() / 1000);
  const app = params.get("app") || "ledger";
  const failedTrace = "a".repeat(32),
    successTrace = "b".repeat(32);
  const metric = (name: string, value: number, labels = {}) => ({
    metric: {
      task_name: name,
      task_framework: "taskiq",
      task_queue: "imports",
      ...labels,
    },
    value: [now, String(value)],
  });
  const source = (rows: unknown[]) => ({ state: "ok", rows });
  if (view === "overview")
    return {
      state: "ok",
      sampled_at: now,
      sources: {
        registered: source(
          [
            "demo.invoice.import",
            "demo.report.generate",
            "demo.search.refresh",
          ].map((name) => metric(name, 1)),
        ),
        active: source([
          metric("demo.invoice.import", 2),
          metric("demo.report.generate", 1),
          metric("demo.search.refresh", 0),
        ]),
        oldest: source([metric("demo.invoice.import", 47.2)]),
        no_progress: source([metric("demo.invoice.import", 8.4)]),
        executions: source([
          metric("demo.invoice.import", 127.4, { outcome: "succeeded" }),
          metric("demo.invoice.import", 3, { outcome: "failed" }),
          metric("demo.invoice.import", 4.2, { outcome: "retrying" }),
          metric("demo.report.generate", 19.2, { outcome: "succeeded" }),
        ]),
        duration: source([
          metric("demo.invoice.import", 1.82),
          metric("demo.report.generate", 7.63),
        ]),
        wait: source([metric("demo.invoice.import", 0.14)]),
        last_finished: source([
          metric("demo.invoice.import", now - 31, { outcome: "succeeded" }),
        ]),
        processes: source([
          {
            metric: {
              __name__: "worker_heartbeat_seconds",
              service_instance_id: "demo-worker-01",
              service_name: `${app}-worker`,
              task_role: "worker",
              task_queue: "imports",
            },
            value: [now, String(now - 8)],
          },
        ]),
        drops: source([]),
      },
    };
  const event = (
    trace_id: string,
    retry: number,
    name: string,
    offset: number,
    extra = {},
  ): TaskEvent => ({
    event: name,
    timestamp: now - 120 + offset,
    task_name: "demo.invoice.import",
    task_id: "demo-task-42",
    trace_id,
    span_id: "1".repeat(16),
    retry,
    runtime: "taskiq",
    queue: "imports",
    ...extra,
  });
  const events = [
    event(failedTrace, 0, "task_started", 0),
    event(failedTrace, 0, "phase_finished", 1, {
      stage: "invoice.read",
      phase_started_at: now - 120,
      phase_finished_at: now - 119,
      phase_duration_ms: 180,
      phase_count: 1,
      phase_outcome: "succeeded",
    }),
    event(failedTrace, 0, "task_failure", 2, {
      stage: "invoice.write",
      error_type: "TimeoutError",
      error_message: "Demo: database statement exceeded the request budget",
      error_location: "demo/import_invoice.py:84",
    }),
    event(failedTrace, 0, "task_finished", 3, {
      outcome: "retrying",
      elapsed_seconds: 2.84,
    }),
    event(successTrace, 1, "task_started", 60),
    event(successTrace, 1, "phase_finished", 61, {
      stage: "invoice.read",
      phase_started_at: now - 60,
      phase_finished_at: now - 59,
      phase_duration_ms: 142,
      phase_count: 1,
      phase_outcome: "succeeded",
    }),
    event(successTrace, 1, "phase_finished", 62, {
      stage: "invoice.write",
      phase_started_at: now - 59,
      phase_finished_at: now - 58,
      phase_duration_ms: 640,
      phase_count: 3,
      phase_outcome: "succeeded",
    }),
    event(successTrace, 1, "task_finished", 63, {
      outcome: "succeeded",
      elapsed_seconds: 1.82,
    }),
  ];
  if (view === "events")
    return {
      state: "ok",
      items: params.get("task_name")?.includes("empty")
        ? []
        : params.get("outcome") === "problem"
          ? events.filter(
              (e) => e.outcome === "retrying" || e.event === "task_failure",
            )
          : events,
      until: now,
      next_offset: null,
    };
  if (view === "traces")
    return {
      state: "ok",
      items: [failedTrace, successTrace].map((traceID, i) => ({
        traceID,
        rootServiceName: `${app}-worker`,
        rootTraceName: "demo.invoice.import",
        startTimeUnixNano: String(BigInt(now - 120 + i * 60) * 1000000000n),
        durationMs: i ? 1820 : 2840,
      })),
    };
  if (view === "trace") {
    const id = params.get("trace_id") || failedTrace;
    const origin = BigInt(now - 120) * 1000000000n;
    const span = (
      name: string,
      index: number,
      parent: number,
      offset: number,
      duration: number,
      error = false,
    ): TraceSpan => ({
      spanId: String(index).repeat(16),
      parentSpanId: parent ? String(parent).repeat(16) : "",
      name,
      service: `${app}-worker`,
      kind: "1",
      status: error ? "error" : "ok",
      startTimeUnixNano: String(origin + BigInt(offset) * 1000000n),
      endTimeUnixNano: String(origin + BigInt(offset + duration) * 1000000n),
      attributes: {
        "task.name": "demo.invoice.import",
        "task.queue": "imports",
        ...(error ? { "error.type": "TimeoutError" } : {}),
      },
      resource: { "luma.stack": app, "service.name": `${app}-worker` },
      events: error
        ? [
            {
              name: "exception",
              timeUnixNano: String(origin + 1100000000n),
              attributes: { "exception.type": "TimeoutError" },
            },
          ]
        : [],
      links:
        index === 1
          ? [
              {
                traceId: id === failedTrace ? successTrace : failedTrace,
                spanId: "1".repeat(16),
              },
            ]
          : [],
      droppedAttributes: 0,
      detailsTruncated: false,
    });
    return {
      state: "ok",
      traceId: id,
      spans: [
        span("demo.invoice.import", 1, 0, 0, 1820),
        span("invoice.read", 2, 1, 30, 142),
        span("invoice.write", 3, 1, 220, 640, id === failedTrace),
        span("postgres.query", 4, 3, 250, 540),
        span("cache.invalidate", 5, 1, 980, 62),
      ],
    };
  }
  return {
    state: "ok",
    diagnostic: {
      error_type: "TimeoutError",
      error_message: "Demo exception for local preview",
      stacktrace:
        'Traceback (demo):\n  File "demo/import_invoice.py", line 84\n    await save_invoice(invoice)\nTimeoutError: database statement exceeded budget',
      truncated: false,
    },
    items: [
      {
        timestamp: new Date((now - 120) * 1000).toISOString(),
        task: "demo-worker",
        stream: "stderr",
        matched: true,
        line: "Demo task failure · " + params.get("trace_id"),
      },
    ],
  };
}
