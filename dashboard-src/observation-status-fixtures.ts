// Development-only states for visual and interaction verification.
export function observationStatusFixture(params: URLSearchParams) {
  const now = Math.floor(Date.now() / 1000),
    app = params.get("app");
  const signal = (value: number) => ({ state: "ok", value, items: [] });
  return {
    controlAlerts: {
      state: "ok",
      counts: { firing: 1, pending: 0 },
      enabledRules: 4,
    },
    state: "ok",
    sampledAt: now,
    historical: Number(params.get("until") || now) < now - 120,
    signals: {
      input_metrics: signal(329.2),
      input_spans: signal(4.1),
      refused: signal(0),
      export_failures: signal(0),
      queue_ratio: signal(0.016),
      log_drops: signal(0),
      unresolved: signal(2),
      query_p99: signal(0.033),
      logs_bytes: signal(2.62 * 1024 ** 3),
      logs_limit: signal(4 * 1024 ** 3),
      free_bytes: signal(19.2 * 1024 ** 3),
      tempo_poll_errors: signal(3),
      log_backlog_bytes: signal(65536),
      freshness: signal(25),
      http_error_ratio: signal(0.003),
      http_p95: signal(0.128),
      task_error_ratio: signal(0.007),
      task_p95: signal(1.82),
      active: signal(3),
      heartbeat_age: signal(25),
      targets: {
        state: "ok",
        items: ["collector", "victoria", "tempo", "victoria-logs"].map(
          (job) => ({ labels: { job }, value: 1 }),
        ),
      },
    },
    coverage: {
      state: "ok",
      items: [
        { app: app || "ledger", task: "worker", at: now - 12, gapAt: 0 },
        { app: app || "ledger", task: "scheduler", at: 0, gapAt: now - 90 },
      ],
    },
  };
}
